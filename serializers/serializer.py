from decimal import Decimal
from django.core.serializers.base import DeserializedObject
from django.utils.datastructures import SortedDict
import copy
import datetime
import types
from serializers.renderers import (
    JSONRenderer,
    YAMLRenderer,
    XMLRenderer,
    HTMLRenderer,
    CSVRenderer,
)
from serializers.parsers import (
    JSONParser,
)
from serializers.fields import *
from serializers.utils import SortedDictWithMetadata, is_simple_callable
from StringIO import StringIO
from io import BytesIO


def _is_protected_type(obj):
    """
    True if the object is a native datatype that does not need to
    be serialized further.
    """
    return isinstance(obj, (
        types.NoneType,
       int, long,
       datetime.datetime, datetime.date, datetime.time,
       float, Decimal,
       basestring)
    )


def _get_declared_fields(bases, attrs):
    """
    Create a list of serializer field instances from the passed in 'attrs',
    plus any similar fields on the base classes (in 'bases').

    Note that all fields from the base classes are used.
    """
    fields = [(field_name, attrs.pop(field_name))
              for field_name, obj in attrs.items()
              if isinstance(obj, Field)]
    fields.sort(key=lambda x: x[1].creation_counter)

    # If this class is subclassing another Serializer, add that Serializer's
    # fields.  Note that we loop over the bases in *reverse*. This is necessary
    # in order to the correct order of fields.
    for base in bases[::-1]:
        if hasattr(base, 'base_fields'):
            fields = base.base_fields.items() + fields

    return SortedDict(fields)


def _get_option(name, kwargs, meta, default):
    return kwargs.get(name, getattr(meta, name, default))


class SerializerOptions(object):
    def __init__(self, meta, **kwargs):
        self.nested = getattr(meta, 'nested', False)
        self.fields = getattr(meta, 'fields', ())
        self.exclude = getattr(meta, 'exclude', ())
        self.is_root = _get_option('is_root', kwargs, meta, False)
        self.renderer_classes = getattr(meta, 'renderer_classes', {
            'xml': XMLRenderer,
            'json': JSONRenderer,
            'yaml': YAMLRenderer,
            'csv': CSVRenderer,
            'html': HTMLRenderer,
        })
        self.parser_classes = getattr(meta, 'parser_classes', {
            'json': JSONParser
        })


class ModelSerializerOptions(SerializerOptions):
    def __init__(self, meta, **kwargs):
        super(ModelSerializerOptions, self).__init__(meta, **kwargs)
        self.model = _get_option('model', kwargs, meta, None)


class SerializerMetaclass(type):
    def __new__(cls, name, bases, attrs):
        attrs['base_fields'] = _get_declared_fields(bases, attrs)
        return super(SerializerMetaclass, cls).__new__(cls, name, bases, attrs)


class BaseSerializer(Field):
    class Meta(object):
        pass

    _options_class = SerializerOptions
    _dict_class = SortedDictWithMetadata  # Set to False for backwards compatability with unsorted implementations.
    internal_use_only = False  # Backwards compatability

    def getvalue(self):
        return self.value  # Backwards compatability with serialization API.

    def __init__(self, label=None, **kwargs):
        super(BaseSerializer, self).__init__(label=label)
        self.fields = copy.deepcopy(self.base_fields)
        self.opts = self._options_class(self.Meta, **kwargs)
        self.parent = None
        self.root = None

    #####
    # Methods to determine which fields to use when (de)serializing objects.

    def default_fields(self, obj, cls, nested):
        """
        Return the complete set of default fields for the object, as a dict.
        """
        return {}

    def get_fields(self, obj, cls, nested):
        """
        Returns the complete set of fields for the object, as a dict.

        This will be the set of any explicitly declared fields,
        plus the set of fields returned by get_default_fields().
        """
        ret = SortedDict()

        # Get the explicitly declared fields
        for key, field in self.fields.items():
            ret[key] = field
            # Determine if the declared field corrosponds to a model field.
            try:
                if key == 'pk':
                    model_field = obj._meta.pk
                else:
                    model_field = obj._meta.get_field_by_name(key)[0]
            except:
                model_field = None
            # Set up the field
            field.initialize(parent=self, model_field=model_field)

        # Add in the default fields
        fields = self.default_fields(obj, cls, nested)
        for key, val in fields.items():
            if key not in ret:
                ret[key] = val

        # If 'fields' is specified, use those fields, in that order.
        if self.opts.fields:
            new = SortedDict()
            for key in self.opts.fields:
                new[key] = ret[key]
            ret = new

        # Remove anything in 'exclude'
        if self.opts.exclude:
            for key in self.opts.exclude:
                ret.pop(key, None)

        return ret

    #####
    # Field methods - used when the serializer class is itself used as a field.

    def initialize(self, parent, model_field=None):
        """
        Same behaviour as usual Field, except that we need to keep track
        of state so that we can deal with handling maximum depth and recursion.
        """
        super(BaseSerializer, self).initialize(parent, model_field)
        self.stack = parent.stack[:]
        if parent.opts.nested and not isinstance(parent.opts.nested, bool):
            self.opts.nested = parent.opts.nested - 1
        else:
            self.opts.nested = parent.opts.nested

    def field_to_native(self, obj, field_name):
        self.obj = obj
        self.field_name = field_name

        if self.opts.is_root:
            return self.to_native(obj)
        return super(BaseSerializer, self).field_to_native(obj, field_name)

    def field_from_native(self, data, field_name, into):
        self.data = data

        field_data = self.from_native(data.get(field_name))
        if self.opts.is_root:
            into.update(field_data)
        else:
            into[field_name] = field_data

    #####
    # Methods to convert or revert from objects <--> primative representations.

    def convert_field_key(self, obj, field_name, field):
        """
        Return the key that should be used for a given field.
        """
        if getattr(field, 'label', None):
            return field.label
        return field_name

    def determine_recursion(self, obj):
        if obj in self.stack and not self.opts.is_root:
            return True
        self.stack.append(obj)

    def convert_object(self, obj):
        if self.determine_recursion(obj):
            fields = self.get_fields(self.obj, None, nested=False)
            field = fields[self.field_name]
            return field.field_to_native(self.obj, self.field_name)

        ret = self._dict_class()

        fields = self.get_fields(obj, None, nested=self.opts.nested)
        for field_name, field in fields.items():
            key = self.convert_field_key(obj, field_name, field)
            value = field.field_to_native(obj, field_name)
            ret.set_with_metadata(key, value, field)
        return ret

    def revert_fields(self, data, cls):
        fields = self.get_fields(None, cls, nested=self.opts.nested)
        reverted_data = {}
        for field_name, field in fields.items():
            field.field_from_native(data, field_name, reverted_data)
        return reverted_data

    def revert_class(self, data):
        if self.opts.is_root:
            return self.parent.revert_class(self.data)
        return None

    def create_object(self, cls, attrs):
        """
        Deserialize a set of attributes into an object instance.
        """
        return attrs

    def to_native(self, obj):
        """
        First stage of serialization.  Objects -> Primatives.
        """
        if _is_protected_type(obj):
            return obj
        elif is_simple_callable(obj):
            return self.to_native(obj())
        elif isinstance(obj, dict):
            return dict([(key, self.to_native(val))
                         for (key, val) in obj.items()])
        elif hasattr(obj, '__iter__'):
            return (self.to_native(item) for item in obj)
        return self.convert_object(obj)

    def from_native(self, data):
        """
        Reverse first stage of serialization.  Primatives -> Objects.
        """
        if _is_protected_type(data):
            return data
        elif hasattr(data, '__iter__') and not isinstance(data, dict):
            return (self.from_native(item) for item in data)
        else:
            cls = self.revert_class(data)
            attrs = self.revert_fields(data, cls)
            return self.create_object(cls, attrs)

    def render(self, data, stream, format, **opts):
        """
        Second stage of serialization.  Primatives -> Bytestream.
        """
        renderer = self.opts.renderer_classes[format]()
        return renderer.render(data, stream, **opts)

    def parse(self, stream, format):
        """
        Reverse the second stage of serialization.  Bytestream -> Primatives.
        """
        parser = self.opts.parser_classes[format]()
        return parser.parse(stream)

    def serialize(self, format, obj, **opts):
        """
        Perform serialization of objects into bytestream.
        First converts the objects into primatives, then renders to bytestream.
        """
        self.stack = []

        # If one of our fields has 'is_root' set, pass through some of our args
        has_root_field = False
        for key, field in self.fields.items():
            if hasattr(field, 'opts') and getattr(field.opts, 'is_root', None):
                has_root_field = True
                for keyword in ('fields', 'exclude', 'nested'):
                    if keyword in opts:
                        setattr(field.opts, keyword, opts.pop(keyword))

        if not has_root_field:
            for keyword in ('fields', 'exclude', 'nested'):
                if keyword in opts:
                    setattr(self.opts, keyword, opts.pop(keyword))

        data = self.to_native(obj)
        if format != 'python':
            stream = opts.pop('stream', StringIO())
            self.render(data, stream, format, **opts)
            if hasattr(stream, 'getvalue'):
                self.value = stream.getvalue()
            else:
                self.value = None
        else:
            self.value = data
        return self.value

    def deserialize(self, format, stream_or_string):
        """
        Perform deserialization of bytestream into objects.
        First parses the bytestream into primative types,
        then reverts into objects.
        """
        self.stack = []

        if format != 'python':
            if isinstance(stream_or_string, basestring):
                stream = BytesIO(stream_or_string)
            else:
                stream = stream_or_string
            data = self.parse(stream, format)
        else:
            data = stream_or_string
        return self.from_native(data)


class Serializer(BaseSerializer):
    __metaclass__ = SerializerMetaclass


class ObjectSerializer(Serializer):
    def default_fields(self, obj, cls, nested):
        """
        Given an object, return the default set of fields to serialize.
        """
        if obj is None:
            raise Exception('ObjectSerializer does not support deserialization')

        ret = SortedDict()
        attrs = [key for key in obj.__dict__.keys() if not(key.startswith('_'))]
        for attr in sorted(attrs):
            if nested:
                field = self.__class__()
            else:
                field = Field()
            field.initialize(parent=self)
            ret[attr] = field
        return ret


class ModelSerializer(Serializer):
    """
    A serializer that deals with model instances and querysets.
    """
    _options_class = ModelSerializerOptions

    def default_fields(self, obj, cls, nested):
        """
        Return all the fields that should be serialized for the model.
        """
        if obj is not None:
            cls = obj.__class__

        opts = cls._meta.concrete_model._meta
        pk_field = opts.pk
        while pk_field.rel:
            pk_field = pk_field.rel.to._meta.pk
        fields = [pk_field]
        fields += [field for field in opts.fields if field.serialize]
        fields += [field for field in opts.many_to_many if field.serialize]

        ret = SortedDict()
        for model_field in fields:
            if model_field.rel and nested:
                field = self.get_nested_field(model_field)
            elif model_field.rel:
                field = self.get_related_field(model_field)
            else:
                field = self.get_field(model_field)
            field.initialize(parent=self, model_field=model_field)
            ret[model_field.name] = field
        return ret

    def get_nested_field(self, model_field):
        """
        Creates a default instance of a nested relational field.
        """
        return ModelSerializer()

    def get_related_field(self, model_field):
        """
        Creates a default instance of a flat relational field.
        """
        return PrimaryKeyRelatedField()

    def get_field(self, model_field):
        """
        Creates a default instance of a basic field.
        """
        return Field()

    def field_to_native(self, obj, field_name):
        if self.opts.is_root:
            return self.to_native(obj)

        obj = getattr(obj, field_name)
        if obj.__class__.__name__ in ('RelatedManager', 'ManyRelatedManager'):
            return [self.to_native(item) for item in obj.all()]
        return self.to_native(obj)

    def revert_class(self, data):
        if self.opts.is_root:
            return self.parent.revert_class(self.data)
        return self.opts.model

    def create_object(self, cls, attrs):
        m2m_data = {}
        for field in cls._meta.many_to_many:
            if field.name in attrs:
                m2m_data[field.name] = attrs.pop(field.name)
        return DeserializedObject(cls(**attrs), m2m_data)
