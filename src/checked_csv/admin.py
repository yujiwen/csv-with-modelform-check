from django.db.models.fields import Field
from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.template.response import TemplateResponse
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy
import csv
import urllib.parse
from .forms import ImportForm
from pandas import pandas as pd
from django.shortcuts import redirect
from django.forms import modelform_factory
from django.core.files.storage import FileSystemStorage
from os import path
import numpy as np
from deepdiff import DeepDiff
from functools import reduce, cache
from itertools import chain, filterfalse
from django.db import transaction
import logging

logging.getLogger(__name__)

class CsvExportModelMixin():
    """
    This is intended to be mixed with django.contrib.admin.ModelAdmin
    https://docs.djangoproject.com/en/3.1/ref/contrib/admin/#modeladmin-objects

    To Export a model's data, mainly supposed to use at django admin site.
    Available setting:
        - file_name: name of the CSV file to Export, if not specified, the model's plural verbose name is used.
        - encoding: Shift-JIS, UTF-8,...etc.
        - dialect: csv.excel or csv.excel_tab. We can customize and register our own dialect.
        - csv_export_fields: using to choose export fields.
        - is_export_verbose_names: whether or not export verbose field names at the first row
        - is_export_field_names: whether or not export field names
        - fmt_params: 
    """
    class csv_quote_all(csv.excel):
        quoting = csv.QUOTE_ALL

    EXPORT_PERMISSION_CODE = 'export'

    file_name = None
    # encoding = 'Shift-JIS'
    encoding = 'UTF-8'
    # dialect = csv.excel
    dialect = csv_quote_all
    csv_export_fields = []
    exclude_csv_export_fields = []
    is_export_verbose_names = False
    is_export_field_names = True

    fmt_params = {}
    
    actions = ['csv_export']
    logger = logging.getLogger(__name__)

    def csv_export(self, request, queryset):
        opts = self.model._meta
    
        def get_csv_export_fields():
            def is_exportable(field):
                return (field.concrete
                    and not getattr(field, 'many_to_many')
                    and (not self.csv_export_fields or field.name in self.csv_export_fields)
                    and (not self.exclude_csv_export_fields or field.name not in self.exclude_csv_export_fields)
                )

            return [f for f in opts.get_fields() if is_exportable(f)]

        filename = self.file_name if self.file_name else urllib.parse.quote(opts.verbose_name_plural + ".csv")
        logging.info(f'Exporting {opts.model_name}.')

        response = HttpResponse(content_type='text/csv; encoding=%s' %(self.encoding) )
        response['Content-Disposition'] = 'attachment; filename=%s' % (filename)
        writer = csv.writer(response, self.dialect)

        csv_field_names = [f.name for f in get_csv_export_fields()]
        if self.is_export_verbose_names:
            writer.writerow([opts.get_field(f).verbose_name.title() for f in csv_field_names ])

        if self.is_export_field_names:
            writer.writerow(csv_field_names)

        for row in queryset.values_list(*csv_field_names):
            writer.writerow(row)
        
        return response

    csv_export.short_description = _('CSV Export')

class CsvImportModelMixin():
    """
    This is intended to be mixed with django.contrib.admin.ModelAdmin
    https://docs.djangoproject.com/en/3.1/ref/contrib/admin/#modeladmin-objects

    CSV import class to import a model's data, mainly supposed to use at django admin site.
    Available setting:
        - csv_import_fields: Field names to import, if not specified, header line is used.
        - csv_excluded_fields: Field names to exclude from import.
        - import_encoding: shift_jis, utf8,...etc.
        - chunk_size: number of rows to be read into a dataframe at a time
        - max_error_rows: maximum number of violation error rows
    """
    csv_import_fields = []
    csv_excluded_fields = []
    unique_check_fields = ()

    import_encoding = 'utf-8'
    # import_encoding = 'shift_jis'

    chunk_size = 10000
    max_error_rows = 1000
    is_skip_existing = False        # True: skip imported row, False: update database with imported row

    is_first_comer_priority = True  # True: Inside a same chunk, first comer is saved to database. False: last was saved

    change_list_template = 'admin/change_list_with_import.html'
    import_template = 'admin/import.html'

    IMPORT_PERMISSION_CODE = 'import'

    def has_import_permission(self, request) -> bool:
        """
        Returns whether a request has import permission.
        """
        import_codename = self.IMPORT_PERMISSION_CODE

        opts = self.model._meta
        if opts.model_name.lower() == 'user' or opts.model_name.lower() == 'group':
            return request.user.has_perm("%s.%s_%s" % (opts.app_label, 'add', opts.model_name))

        return request.user.has_perm("%s.%s_%s" % (opts.app_label, import_codename, opts.model_name))

    def changelist_view(self, request, extra_context=None):
        """
        override of the ModelAdmin
        """
        extra_context = extra_context or {}
        extra_context['has_import_permission'] = self.has_import_permission(request)
        return super(CsvImportModelMixin, self).changelist_view(request, extra_context=extra_context)

    def get_urls(self):
        """
        override of the ModelAdmin
        """
        from django.urls import path

        opts = self.model._meta
        import_url = [
            path('import/', self.admin_site.admin_view(self.import_action), name='%s_%s_import' % (opts.app_label, opts.model_name)),
        ]
        return import_url + super(CsvImportModelMixin, self).get_urls()

    @cache
    def get_csv_import_fields(self) -> list[Field]:
        def is_importable_fields(field):
            return (field.concrete
                and not getattr(field, 'many_to_many')
                and (not self.csv_import_fields or field.name in self.csv_import_fields)
                and (not self.get_csv_excluded_fields() or field.name not in self.get_csv_excluded_fields())
            )

        return [f for f in self.model._meta.get_fields() if is_importable_fields(f)]

    @cache
    def get_unique_check_fields(self) -> tuple[str]:
        if self.unique_check_fields:
            return self.unique_check_fields
        
        opts = self.model._meta
        if opts.unique_together:
            return opts.unique_together
        
        if opts.total_unique_constraints:
            default_unique_contraint_name = '%s_unique' % opts.model_name
            unique_constraints = [c for c in opts.total_unique_constraints if c.name == default_unique_contraint_name]
            if unique_constraints:
                return unique_constraints[0].fields
            else:
                return opts.total_unique_constraints[0].fields
        
        return ()

    def get_csv_excluded_fields(self) -> list[str]:
        """
        Hook for excluding fields to import from csv
        """
        return self.csv_excluded_fields

    def get_csv_excluded_fields_init_values(self, request) -> dict:
        """
        Hook for initializing excluded fields, if necessary, such as 'creator', 'created_at', 'updater'...
        """
        return {}

    def update_csv_excluded_fields(self, request, row) -> None:
        """
        Hook for updating excluded fields, if necessary, such as 'updater', 'updated_at', and 'version'
        """
        pass

    def get_update_fields(self) -> list[str]:
        """
        When a database rocord is duplicated with a imported row, tell which fields should be updated using the csv data. 
        """
        return [f.name for f in self.get_csv_import_fields() if not f.primary_key]

    @transaction.non_atomic_requests
    def import_action(self, request, *args, **kwargs):
        """
        """
        def has_nonunique_violation(modelform):
            """
            Collect all error codes except the unique contraint violation.
            """
            return list(filterfalse(lambda e: e.code in ['unique', 'unique_together'],
                [e for e in chain.from_iterable([errorlist.as_data() for errorlist in modelform.errors.values()])]
            ))

        def get_unique_constraint_violation_fields(modelform) -> tuple[str]:
            error = next(filter(lambda e: e.code in ['unique', 'unique_together'],
                [e for e in chain.from_iterable([errorlist.as_data() for errorlist in modelform.errors.values()])]
            ))

            return error.params['unique_check']

        def exclude_duplication(rows, imported, modelform):
            unique_fields = self.get_unique_check_fields()
            if not unique_fields:
                return imported

            return reduce(lambda p, n: p if self.is_first_comer_priority else n, [
                    row for row in rows if not any(
                        [DeepDiff(getattr(row, f), getattr(imported, f), ignore_order=True) for f in unique_fields]
                    )
                ], imported
            )

        def read_record(request, new_rows: list, update_rows: list, errors: list, record: dict):
            def add_error(errors: list, modelform):
                if len(errors) < self.max_error_rows:
                    errors.append(modelform)

            # Create an instance of the ModelForm class using one record of the csv data
            modelform = modelform_class(record | self.get_csv_excluded_fields_init_values(request))
            if modelform.is_valid():
                # newly imported data
                row = self.model(**(modelform.cleaned_data | self.get_csv_excluded_fields_init_values(request)))
                new_rows.append(exclude_duplication(new_rows, row, modelform))
            else:
                if has_nonunique_violation(modelform):
                    add_error(errors, modelform)
                else:
                    if not self.is_skip_existing:
                        row = self.model.objects.get(**{k : record[k] for k in get_unique_constraint_violation_fields(modelform)})
                        # If we had same record in database, would update it by imported data
                        for k, v in modelform.cleaned_data.items():
                            setattr(row, k, v)
                        self.update_csv_excluded_fields(request, row)
                        update_rows.append(exclude_duplication(update_rows, row, modelform))

        def disable_formfield(db_field, **kwargs):
            form_field = db_field.formfield(**kwargs)
            if form_field:
                form_field.widget.attrs['disabled'] = 'true'
            return form_field

        if not self.has_import_permission(request):
            raise PermissionDenied

        opts = self.model._meta
        title = _('Import %(name)s') % {'name': opts.verbose_name}
        context = {
            **self.admin_site.each_context(request),
            'title': title,
            'app_list': self.admin_site.get_app_list(request),
            'opts': opts,
            'has_view_permission': self.has_view_permission(request),
        }

        if request.method == "GET":
            form = ImportForm()

        if request.method == "POST":
            form = ImportForm(request.POST, request.FILES)
            if form.is_valid():
                import_file = form.cleaned_data['import_file']
                # I have to save the file to file system before read into pandas, otherwise the encoding is ignored by pandas.
                fs = FileSystemStorage('temp')
                file_name = fs.save(import_file.name, import_file)
                file_path = path.join(fs.location, file_name)
                logging.info(f'Importing {opts.model_name} from {file_path}.')

                model_field_names = [f.name for f in self.get_csv_import_fields()]
                # Dynamically generate ModelForm class
                # modelform_class = globals()[opts.object_name + 'Form']
                modelform_class = modelform_factory(self.model, fields = model_field_names, formfield_callback = disable_formfield)

                read_csv_params = {'encoding' : self.import_encoding, 
                                   'chunksize' : self.chunk_size,
                                   'na_filter' : False,
                                   'dtype' : 'str'
                                  }
                errors = []
                try:
                    for chunk in pd.read_csv(file_path, **read_csv_params):
                        # df = chunk.replace(np.nan, '', regex=True)
                        # df = chunk.applymap(str)
                        new_rows = []
                        update_rows = []
                        with transaction.atomic():
                            for record in chunk.to_dict('record'):
                                read_record(request, new_rows, update_rows, errors, record)

                            if new_rows:
                                self.model.objects.bulk_create(new_rows)
                                logging.info(f'There are {len(new_rows)} new rows were imported to {opts.model_name}.')
                            if update_rows:
                                self.model.objects.bulk_update(update_rows, self.get_update_fields())
                                logging.info(f'There are {len(update_rows)} rows were updated to {opts.model_name}.')
                except Exception as e:      #todo: write exception info to log file
                    logging.exception(e)
                    raise e
                finally:
                    fs.delete(file_name)

                if errors:
                    context['title'] = _('%(name)s import errors')% {'name': opts.verbose_name}
                    context['show_close'] = True
                    context['forms'] = errors
                    logging.info(f'There are {len(errors)} error records at {file_path} when importing it to {opts.model_name}.')
                    return TemplateResponse(request, 'admin/import_error.html', context)
                else:
                    # return super(CsvImportModelMixin, self).changelist_view(request=request)
                    return redirect(reverse_lazy('%s:%s_%s_changelist' % 
                        (request.resolver_match.namespace, opts.app_label, opts.model_name)))

        context['form'] = form
        return TemplateResponse(request, 'admin/import.html', context)

