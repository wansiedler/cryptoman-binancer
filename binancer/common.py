import os
import platform
import json

from django.db.transaction import Atomic
from django.forms import model_to_dict

import sys
import traceback

from decimal import Decimal


def cfloat(f):
    d = Decimal(str(f))
    d = d.quantize(Decimal(1)) if d == d.to_integral() else d.normalize()
    # return f'{d}'
    return f'{d:.8f}'


def intersect(a, b):
    return set(a).intersection(b)


def contains_only(a, b):
    return len(a) > 0 and intersect(a, b) == a


def format_stacktrace():
    parts = ["Traceback:\n"]
    parts.extend(traceback.format_stack(limit=25)[:-2])
    parts.extend(traceback.format_exception(*sys.exc_info())[1:])
    return "".join(parts)


def format_json(message):
    return json.dumps(message, indent=4, sort_keys=True)


dump_json = format_json


class ModelDiffMixin(object):
    """
    A model mixin that tracks model fields' values and provide some useful api
    to know what fields have been changed.
    """

    def __init__(self, *args, **kwargs):
        super(ModelDiffMixin, self).__init__(*args, **kwargs)
        self.__initial = self._dict

    @property
    def diff(self):
        d1 = self.__initial
        d2 = self._dict
        diffs = [(k, (v, d2[k])) for k, v in d1.items() if v != d2[k]]
        return dict(diffs)

    @property
    def has_changed(self):
        return bool(self.diff)

    @property
    def changed_fields(self):
        return self.diff.keys()

    def get_field_diff(self, field_name):
        """
        Returns a diff for field if it's changed and None otherwise.
        """
        return self.diff.get(field_name, None)

    def save(self, *args, **kwargs):
        """
        Saves model and set initial state.
        """
        super(ModelDiffMixin, self).save(*args, **kwargs)
        self.__initial = self._dict

    @property
    def _dict(self):
        return model_to_dict(self, fields=[field.name for field in
                                           self._meta.fields])


class MakeDirty(object):
    def __init__(self, obj):
        self.obj = obj

    def __enter__(self):
        self.obj._dirty = True

    def __exit__(self, exc_type, exc_val, exc_tb):
        delattr(self.obj, '_dirty')


def is_dirty(obj):
    return hasattr(obj, '_dirty')


def creation_date(path_to_file):
    """
    Try to get the date that a file was created, falling back to when it was
    last modified if that isn't possible.
    See http://stackoverflow.com/a/39501288/1709587 for explanation.
    """
    if platform.system() == 'Windows':
        return os.path.getctime(path_to_file)
    else:
        stat = os.stat(path_to_file)
        try:
            return stat.st_birthtime
        except AttributeError:
            # We're probably on Linux. No easy way to get creation dates here,
            # so we'll settle for when its content was last modified.
            return stat.st_mtime


class ReadOnlyFieldsMixin(object):
    readonly_fields = ()

    def __init__(self, *args, **kwargs):
        super(ReadOnlyFieldsMixin, self).__init__(*args, **kwargs)
        for field in (field for name, field in self.fields.items() if name in self.readonly_fields):
            field.widget.attrs['disabled'] = 'true'
            field.required = False

    def clean(self):
        cleaned_data = super(ReadOnlyFieldsMixin, self).clean()
        for field in self.readonly_fields:
            cleaned_data[field] = getattr(self.instance, field)

        return cleaned_data


class lock_instance(Atomic):
    def __init__(self, instance):
        super().__init__(None, True)
        self.instance = instance

    def __enter__(self):
        super().__enter__()
        return self.instance.__class__.objects.select_for_update().get(id=self.instance.id)

    def __exit__(self, *args, **kwargs):
        super().__exit__(*args, **kwargs)


def copy_attrs(dst, src, attrs=None):
    if not attrs:
        dst.update(src)
    else:
        for attr in attrs.split(','):
            dst[attr] = src[attr]
