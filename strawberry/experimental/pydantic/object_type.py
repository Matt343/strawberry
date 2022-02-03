from __future__ import annotations

import builtins
import dataclasses
import warnings
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Type,
    cast,
)

from pydantic import BaseModel
from pydantic.fields import ModelField
from typing_extensions import Literal

from graphql import GraphQLResolveInfo

import strawberry
from strawberry.arguments import UNSET
from strawberry.experimental.pydantic.conversion import (
    convert_pydantic_model_to_strawberry_class,
    convert_strawberry_class_to_pydantic_model,
)
from strawberry.experimental.pydantic.fields import get_basic_type
from strawberry.experimental.pydantic.utils import (
    DataclassCreationFields,
    ensure_all_auto_fields_in_pydantic,
    get_default_factory_for_field,
    get_private_fields,
    sort_creation_fields,
)
from strawberry.field import StrawberryField
from strawberry.object_type import _process_type, _wrap_dataclass
from strawberry.schema_directive import StrawberrySchemaDirective
from strawberry.types.type_resolver import _get_fields
from strawberry.types.types import TypeDefinition

from .exceptions import MissingFieldsListError, UnregisteredTypeException


def replace_pydantic_types(type_: Any, is_input: bool):
    origin = getattr(type_, "__origin__", None)
    if origin is Literal:
        # Literal does not have types in its __args__ so we return early
        return type_
    if hasattr(type_, "__args__"):
        replaced_type = type_.copy_with(
            tuple(replace_pydantic_types(t, is_input) for t in type_.__args__)
        )

        if isinstance(replaced_type, TypeDefinition):
            # TODO: Not sure if this is necessary. No coverage in tests
            # TODO: Unnecessary with StrawberryObject

            replaced_type = builtins.type(
                replaced_type.name,
                (),
                {"_type_definition": replaced_type},
            )

        return replaced_type

    if issubclass(type_, BaseModel):
        attr = "_strawberry_input_type" if is_input else "_strawberry_type"
        if hasattr(type_, attr):
            return getattr(type_, attr)
        else:
            raise UnregisteredTypeException(type_)

    return type_


def get_type_for_field(field: ModelField, is_input: bool):
    type_ = field.outer_type_
    type_ = get_basic_type(type_)
    type_ = replace_pydantic_types(type_, is_input)

    if not field.required:
        type_ = Optional[type_]

    return type_


def _build_dataclass_creation_fields(
    field: ModelField,
    is_input: bool,
    existing_fields: Dict[str, StrawberryField],
    auto_fields_set: Set[str],
) -> DataclassCreationFields:
    type_annotation = (
        get_type_for_field(field, is_input)
        if field.name in auto_fields_set
        else existing_fields[field.name].type
    )

    if (
        field.name in existing_fields
        and existing_fields[field.name].base_resolver is not None
    ):
        # if the user has defined a resolver for this field, always use it
        strawberry_field = existing_fields[field.name]
    else:
        # otherwise we build an appropriate strawberry field that resolves it
        strawberry_field = StrawberryField(
            python_name=field.name,
            graphql_name=field.alias if field.has_alias else None,
            # always unset because we use default_factory instead
            default=UNSET,
            default_factory=get_default_factory_for_field(field),
            type_annotation=type_annotation,
            description=field.field_info.description,
        )

    return DataclassCreationFields(
        name=field.name,
        type_annotation=type_annotation,
        field=strawberry_field,
    )


if TYPE_CHECKING:
    from strawberry.experimental.pydantic.conversion_types import (
        PydanticModel,
        StrawberryTypeFromPydantic,
    )


def type(
    model: Type[PydanticModel],
    *,
    fields: Optional[List[str]] = None,
    name: Optional[str] = None,
    is_input: bool = False,
    is_interface: bool = False,
    description: Optional[str] = None,
    directives: Optional[Sequence[StrawberrySchemaDirective]] = (),
    all_fields: bool = False,
    from_pydantic: Optional[
        Callable[
            [PydanticModel, Dict[str, Any]],
            StrawberryTypeFromPydantic[PydanticModel],
        ]
    ] = None,
    to_pydantic: Optional[
        Callable[[StrawberryTypeFromPydantic[PydanticModel]], PydanticModel]
    ] = None,
) -> Callable[..., Type[StrawberryTypeFromPydantic[PydanticModel]]]:
    def wrap(cls: Any) -> Type[StrawberryTypeFromPydantic[PydanticModel]]:
        model_fields = model.__fields__
        original_fields_set = set(fields) if fields else set([])

        if fields:
            warnings.warn(
                "`fields` is deprecated, use `auto` type annotations instead",
                DeprecationWarning,
            )

        existing_fields = getattr(cls, "__annotations__", {})
        # these are the fields that matched a field name in the pydantic model
        # and should copy their alias from the pydantic model
        fields_set = original_fields_set.union(
            set(name for name, _ in existing_fields.items() if name in model_fields)
        )
        # these are the fields that were marked with strawberry.auto and
        # should copy their type from the pydantic model
        auto_fields_set = original_fields_set.union(
            set(name for name, typ in existing_fields.items() if typ == strawberry.auto)
        )

        if all_fields:
            if fields_set:
                warnings.warn(
                    "Using all_fields overrides any explicitly defined fields "
                    "in the model, using both is likely a bug",
                    stacklevel=2,
                )
            fields_set = set(model_fields.keys())
            auto_fields_set = set(model_fields.keys())

        if not fields_set:
            raise MissingFieldsListError(cls)

        ensure_all_auto_fields_in_pydantic(
            model=model, auto_fields=auto_fields_set, cls_name=cls.__name__
        )

        wrapped = _wrap_dataclass(cls)
        extra_strawberry_fields = _get_fields(wrapped)
        extra_fields = cast(List[dataclasses.Field], extra_strawberry_fields)
        private_fields = get_private_fields(wrapped)

        extra_fields_dict = {field.name: field for field in extra_strawberry_fields}

        all_model_fields: List[DataclassCreationFields] = [
            _build_dataclass_creation_fields(
                field, is_input, extra_fields_dict, auto_fields_set
            )
            for field_name, field in model_fields.items()
            if field_name in fields_set
        ]

        all_model_fields.extend(
            (
                DataclassCreationFields(
                    name=field.name,
                    type_annotation=field.type,
                    field=field,
                )
                for field in extra_fields + private_fields
                if field.name not in fields_set
            )
        )

        # Sort fields so that fields with missing defaults go first
        sorted_fields = sort_creation_fields(all_model_fields)

        # Implicitly define `is_type_of` to support interfaces/unions that use
        # pydantic objects (not the corresponding strawberry type)
        @classmethod  # type: ignore
        def is_type_of(cls: Type, obj: Any, _info: GraphQLResolveInfo) -> bool:
            return isinstance(obj, (cls, model))

        cls = dataclasses.make_dataclass(
            cls.__name__,
            [field.to_tuple() for field in sorted_fields],
            bases=cls.__bases__,
            namespace={"is_type_of": is_type_of},
        )

        _process_type(
            cls,
            name=name,
            is_input=is_input,
            is_interface=is_interface,
            description=description,
            directives=directives,
        )

        if is_input:
            model._strawberry_input_type = cls  # type: ignore
        else:
            model._strawberry_type = cls  # type: ignore
        cls._pydantic_type = model  # type: ignore

        def from_pydantic_default(
            instance: PydanticModel, extra: Dict[str, Any] = None
        ) -> StrawberryTypeFromPydantic[PydanticModel]:
            return convert_pydantic_model_to_strawberry_class(
                cls=cls, model_instance=instance, extra=extra
            )

        def to_pydantic_default(self) -> PydanticModel:
            instance_kwargs = {
                f.name: convert_strawberry_class_to_pydantic_model(
                    getattr(self, f.name)
                )
                for f in dataclasses.fields(self)
            }
            return model(**instance_kwargs)

        cls.from_pydantic = from_pydantic or staticmethod(from_pydantic_default)
        cls.to_pydantic = to_pydantic or to_pydantic_default

        return cls

    return wrap


input = partial(type, is_input=True)

interface = partial(type, is_interface=True)
