from django.contrib.auth.admin import GroupAdmin, UserAdmin
from checked_csv.admin import CsvExportModelMixin, CsvImportModelMixin
from django.contrib import admin
from django.contrib.auth import models as auth


class UserModelAdmin(CsvExportModelMixin, CsvImportModelMixin, UserAdmin):
    pass

admin.site.unregister(auth.User)

admin.site.register(auth.User, UserModelAdmin)
