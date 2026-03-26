from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper

DESCRIPTOR: _descriptor.FileDescriptor

class GraphCodeIndex(_message.Message):
    __slots__ = ()
    NODES_FIELD_NUMBER: _ClassVar[int]
    RELATIONSHIPS_FIELD_NUMBER: _ClassVar[int]
    nodes: _containers.RepeatedCompositeFieldContainer[Node]
    relationships: _containers.RepeatedCompositeFieldContainer[Relationship]
    def __init__(
        self,
        nodes: _Iterable[Node | _Mapping] | None = ...,
        relationships: _Iterable[Relationship | _Mapping] | None = ...,
    ) -> None: ...

class Node(_message.Message):
    __slots__ = ()
    PROJECT_FIELD_NUMBER: _ClassVar[int]
    PACKAGE_FIELD_NUMBER: _ClassVar[int]
    FOLDER_FIELD_NUMBER: _ClassVar[int]
    MODULE_FIELD_NUMBER: _ClassVar[int]
    CLASS_NODE_FIELD_NUMBER: _ClassVar[int]
    FUNCTION_FIELD_NUMBER: _ClassVar[int]
    METHOD_FIELD_NUMBER: _ClassVar[int]
    FILE_FIELD_NUMBER: _ClassVar[int]
    EXTERNAL_PACKAGE_FIELD_NUMBER: _ClassVar[int]
    MODULE_IMPLEMENTATION_FIELD_NUMBER: _ClassVar[int]
    MODULE_INTERFACE_FIELD_NUMBER: _ClassVar[int]
    project: Project
    package: Package
    folder: Folder
    module: Module
    class_node: Class
    function: Function
    method: Method
    file: File
    external_package: ExternalPackage
    module_implementation: ModuleImplementation
    module_interface: ModuleInterface
    def __init__(
        self,
        project: Project | _Mapping | None = ...,
        package: Package | _Mapping | None = ...,
        folder: Folder | _Mapping | None = ...,
        module: Module | _Mapping | None = ...,
        class_node: Class | _Mapping | None = ...,
        function: Function | _Mapping | None = ...,
        method: Method | _Mapping | None = ...,
        file: File | _Mapping | None = ...,
        external_package: ExternalPackage | _Mapping | None = ...,
        module_implementation: ModuleImplementation | _Mapping | None = ...,
        module_interface: ModuleInterface | _Mapping | None = ...,
    ) -> None: ...

class Relationship(_message.Message):
    __slots__ = ()
    class RelationshipType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        RELATIONSHIP_TYPE_UNSPECIFIED: _ClassVar[Relationship.RelationshipType]
        CONTAINS_PACKAGE: _ClassVar[Relationship.RelationshipType]
        CONTAINS_FOLDER: _ClassVar[Relationship.RelationshipType]
        CONTAINS_FILE: _ClassVar[Relationship.RelationshipType]
        CONTAINS_MODULE: _ClassVar[Relationship.RelationshipType]
        DEFINES: _ClassVar[Relationship.RelationshipType]
        DEFINES_METHOD: _ClassVar[Relationship.RelationshipType]
        IMPORTS: _ClassVar[Relationship.RelationshipType]
        INHERITS: _ClassVar[Relationship.RelationshipType]
        OVERRIDES: _ClassVar[Relationship.RelationshipType]
        CALLS: _ClassVar[Relationship.RelationshipType]
        DEPENDS_ON_EXTERNAL: _ClassVar[Relationship.RelationshipType]
        IMPLEMENTS_MODULE: _ClassVar[Relationship.RelationshipType]
        IMPLEMENTS: _ClassVar[Relationship.RelationshipType]

    RELATIONSHIP_TYPE_UNSPECIFIED: Relationship.RelationshipType
    CONTAINS_PACKAGE: Relationship.RelationshipType
    CONTAINS_FOLDER: Relationship.RelationshipType
    CONTAINS_FILE: Relationship.RelationshipType
    CONTAINS_MODULE: Relationship.RelationshipType
    DEFINES: Relationship.RelationshipType
    DEFINES_METHOD: Relationship.RelationshipType
    IMPORTS: Relationship.RelationshipType
    INHERITS: Relationship.RelationshipType
    OVERRIDES: Relationship.RelationshipType
    CALLS: Relationship.RelationshipType
    DEPENDS_ON_EXTERNAL: Relationship.RelationshipType
    IMPLEMENTS_MODULE: Relationship.RelationshipType
    IMPLEMENTS: Relationship.RelationshipType
    TYPE_FIELD_NUMBER: _ClassVar[int]
    SOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    TARGET_ID_FIELD_NUMBER: _ClassVar[int]
    PROPERTIES_FIELD_NUMBER: _ClassVar[int]
    SOURCE_LABEL_FIELD_NUMBER: _ClassVar[int]
    TARGET_LABEL_FIELD_NUMBER: _ClassVar[int]
    type: Relationship.RelationshipType
    source_id: str
    target_id: str
    properties: _struct_pb2.Struct
    source_label: str
    target_label: str
    def __init__(
        self,
        type: Relationship.RelationshipType | str | None = ...,
        source_id: str | None = ...,
        target_id: str | None = ...,
        properties: _struct_pb2.Struct | _Mapping | None = ...,
        source_label: str | None = ...,
        target_label: str | None = ...,
    ) -> None: ...

class Project(_message.Message):
    __slots__ = ()
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: str | None = ...) -> None: ...

class Package(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    path: str
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        path: str | None = ...,
    ) -> None: ...

class Folder(_message.Message):
    __slots__ = ()
    PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    path: str
    name: str
    def __init__(self, path: str | None = ..., name: str | None = ...) -> None: ...

class File(_message.Message):
    __slots__ = ()
    PATH_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    EXTENSION_FIELD_NUMBER: _ClassVar[int]
    path: str
    name: str
    extension: str
    def __init__(
        self,
        path: str | None = ...,
        name: str | None = ...,
        extension: str | None = ...,
    ) -> None: ...

class Module(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    path: str
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        path: str | None = ...,
    ) -> None: ...

class ModuleImplementation(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    IMPLEMENTS_MODULE_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    path: str
    implements_module: str
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        path: str | None = ...,
        implements_module: str | None = ...,
    ) -> None: ...

class ModuleInterface(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    PATH_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    path: str
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        path: str | None = ...,
    ) -> None: ...

class ExternalPackage(_message.Message):
    __slots__ = ()
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: str | None = ...) -> None: ...

class Function(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DOCSTRING_FIELD_NUMBER: _ClassVar[int]
    START_LINE_FIELD_NUMBER: _ClassVar[int]
    END_LINE_FIELD_NUMBER: _ClassVar[int]
    DECORATORS_FIELD_NUMBER: _ClassVar[int]
    IS_EXPORTED_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    docstring: str
    start_line: int
    end_line: int
    decorators: _containers.RepeatedScalarFieldContainer[str]
    is_exported: bool
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        docstring: str | None = ...,
        start_line: int | None = ...,
        end_line: int | None = ...,
        decorators: _Iterable[str] | None = ...,
        is_exported: bool | None = ...,
    ) -> None: ...

class Method(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DOCSTRING_FIELD_NUMBER: _ClassVar[int]
    START_LINE_FIELD_NUMBER: _ClassVar[int]
    END_LINE_FIELD_NUMBER: _ClassVar[int]
    DECORATORS_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    docstring: str
    start_line: int
    end_line: int
    decorators: _containers.RepeatedScalarFieldContainer[str]
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        docstring: str | None = ...,
        start_line: int | None = ...,
        end_line: int | None = ...,
        decorators: _Iterable[str] | None = ...,
    ) -> None: ...

class Class(_message.Message):
    __slots__ = ()
    QUALIFIED_NAME_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    DOCSTRING_FIELD_NUMBER: _ClassVar[int]
    START_LINE_FIELD_NUMBER: _ClassVar[int]
    END_LINE_FIELD_NUMBER: _ClassVar[int]
    DECORATORS_FIELD_NUMBER: _ClassVar[int]
    IS_EXPORTED_FIELD_NUMBER: _ClassVar[int]
    qualified_name: str
    name: str
    docstring: str
    start_line: int
    end_line: int
    decorators: _containers.RepeatedScalarFieldContainer[str]
    is_exported: bool
    def __init__(
        self,
        qualified_name: str | None = ...,
        name: str | None = ...,
        docstring: str | None = ...,
        start_line: int | None = ...,
        end_line: int | None = ...,
        decorators: _Iterable[str] | None = ...,
        is_exported: bool | None = ...,
    ) -> None: ...
