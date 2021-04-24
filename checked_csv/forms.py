from django import forms
from django.utils.translation import gettext_lazy as _


class ImportForm(forms.Form):
    import_file = forms.FileField(
        required=True,
        label = _('File to upload')
    )
