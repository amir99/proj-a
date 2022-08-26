# -*- coding: utf-8 -*-
from django import forms
from django.forms.models import inlineformset_factory
from django.utils.translation import ugettext_lazy as _
from django.forms.models import modelformset_factory
from django.forms.models import inlineformset_factory
from django.forms.widgets import Textarea, TextInput
from django.forms.fields import DecimalField, CharField
from django.shortcuts import render,  get_object_or_404, redirect
from django.core.validators import *

from farm.models import Farm

class ImportForm(forms.Form):
   # cats = Category.objects.values_list('id', 'title').all()
   # cat = forms.ChoiceField(cats)
   file = forms.FileField(label='קובץ מחירי עלות')


class TenantForm(forms.Form):
    schema = forms.CharField(label='דומיין אתר', help_text='המילה הראשונה בכתובת, תצורף לסיומת easyfarm.co.il')
    title = forms.CharField(label='כותרת אתר')
    subtitle = forms.CharField(label='כותרת משנה', required=False)
    logo = forms.FileField(label='קובץ גרפי רצועה עליונה', required=False)
    phone = forms.CharField(label='מס\' טלפון')
    from_address = forms.EmailField(label='אימייל')
    
    # fields = ('schema', 'sibte_tu'itle', 'subtitle', 'logo', 'phone', 'from_address',)

class ClubMemberForm(forms.Form):
    club_op = forms.ChoiceField(choices=[
                (1, 'אני חבר/ת מועדון, נרשמתי בעבר'),
                (2, 'מעוניין/ת להירשם למועדון הלקוחות (בתשלום חד פעמי של 20 ש"ח)'),
                (3, 'לא מעוניין/ת להירשם למועדון הלקוחות')
    ], widget=forms.RadioSelect, required=False, label='')
