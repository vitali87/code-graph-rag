# Java same-arity overloads were told apart only by exact, primitive and boxed
# argument types. A cast argument carried no type; an argument whose type is a
# subtype of the parameter (`new LinkedHashMap<>()` for a `Map`, a project
# class for its project superclass or interface) ruled out every candidate;
# overloads inherited from a superclass were never weighed against the
# receiver's own. Each case fell back to the first declaration, and the edge
# still said `exact` (issue #2548).
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java.method_resolver import (
    _java_param_type_names,
    _overload_rank,
)
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import JavaOverloadRank, JavaSupertypes

PROJECT = "jover"

# The issue's repro, verbatim in shape.
NAMES = """package com.acme;

import java.util.function.Supplier;

class Shape {}
class Circle extends Shape {}
class Square extends Shape {}

public final class Names {
  static String name(Circle c, int n) { return "circle" + n; }
  static String name(Square s, int n) { return "square" + n; }

  static String viaParam(Square s) { return name(s, 1); }
  static String viaCast(Shape x) { return name((Square) x, 2); }
  static String viaParenCast(Shape x) { return name(((Square) x), 3); }
  static String viaVarCast(Shape x) {
    var s = (Square) x;
    return name(s, 5);
  }
  static String viaUnknown(Supplier<Square> s) { return name(s.get(), 4); }
}
"""

EXT = """package com.acme;

import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class Ext {
  static String get(List<String> l, Map<String, Integer> m) { return "list"; }
  static String get(Set<String> s, Map<Integer, String> m) { return "set"; }

  static String viaParamExt(Set<String> s) { return get(s, new LinkedHashMap<>()); }
  static String viaParamExt2(Set<String> s, Map<Integer, String> m) { return get(s, m); }

  static String put(Object o) { return "object"; }
  static String put(Map<String, Integer> m) { return "map"; }

  static String viaLibrarySubtype() { return put(new HashMap<String, Integer>()); }
}
"""

HIER = """package com.acme;

class Base2 {
  static String conv(Shape s, int n) { return "base"; }
}

class Stranger {
  static String conv(Square s, int n) { return "stranger"; }
}

public class Hier extends Base2 {
  static String conv(Circle c, int n) { return "child"; }

  static String use(Square s) { return conv(s, 1); }
  static String useCircle(Circle c) { return conv(c, 2); }
}
"""

# Only Base3.java declares the inherited overload: the pool must follow the
# superclass into another file.
BASE3 = """package com.acme;

public class Base3 {
  public String draw(Shape s) { return "base"; }
}
"""

CANVAS = """package com.acme;

public class Canvas extends Base3 {
  public String draw(Circle c) { return "circle"; }

  public String paint(Square s) { return draw(s); }
  public String paintOn(Canvas canvas, Square s) { return canvas.draw(s); }
}
"""

OVERRIDE = """package com.acme;

class Painter {
  String paint(Shape s) { return "painter"; }
}

public class FancyPainter extends Painter {
  @Override String paint(Shape s) { return "fancy"; }

  String run(Square sq) { return paint(sq); }
}
"""

SPECIFIC = """package com.acme;

class Tile extends Square {}

public class Specific {
  static String take(Object o) { return "object"; }
  static String take(Shape s) { return "shape"; }

  static String fit(Shape s) { return "shape"; }
  static String fit(Square s) { return "square"; }

  static String viaSubclass(Square sq) { return take(sq); }
  static String viaNearest(Tile t) { return fit(t); }
}
"""

IFACE = """package com.acme;

interface Api {}
interface Other {}
class Impl implements Api {}

public class Iface {
  static String accept(Other o) { return "other"; }
  static String accept(Api a) { return "api"; }

  static String viaImpl(Impl i) { return accept(i); }
}
"""

PRIM = """package com.acme;

public class Prim {
  static String take(String s) { return "s"; }
  static String take(int n) { return "i"; }

  static String viaPrimitiveCast(double d) { return take((int) d); }
}
"""

PROVEN = """package com.acme;

import java.time.Instant;
import java.time.temporal.Temporal;

class Widget {}

public class Proven {
  static String hold(Object o) { return "object"; }
  static String hold(Widget w) { return "widget"; }

  static String keep(Object o) { return "object"; }
  static String keep(Temporal t) { return "temporal"; }

  static String viaUnrelated(Instant t) { return hold(t); }
  static String viaUnindexedJdkSupertype(Instant t) { return keep(t); }
}
"""

# Type-variable parameters (CodeRabbit on #2800). An unbounded `<T> f(T)` beside
# `f(Object)` would clash by erasure, so the Object-beside-T shapes use bounds.
GENERIC = """package com.acme;

import java.util.function.Supplier;

public class Generic {
  static <T> String f(T x) { return "t"; }
  static String f(String s) { return "s"; }

  static <T extends CharSequence> String g(T x) { return "t"; }
  static String g(Object o) { return "o"; }

  static <T> String put(int k, T v) { return "int"; }
  static <T> String put(String k, T v) { return "string"; }

  static String viaUnknown(Supplier<String> s) { return f(s.get()); }
  static String viaString(String s) { return f(s); }
  static String viaBounded(String s) { return g(s); }
  static String viaTypeVariableArgument() { return put(1, "x"); }
}

class Box<T extends Number> {
  String put(T t) { return "t"; }
  String put(Object o) { return "o"; }
}

class BoxUser {
  String viaInteger(Box<Integer> box, Integer i) { return box.put(i); }
}
"""

# A JDK type outside the supertype table: whether IOException reaches
# Throwable is not visible, so handle(Object) is no certain pick.
FAULTS = """package com.acme;

import java.io.IOException;

class AppError extends Exception {}

class FaultBase {
  String report(Object o) { return "object"; }
}

class FaultChild extends FaultBase {
  String report(Throwable t) { return "throwable"; }

  String viaIo(IOException e) { return report(e); }
}

public class Faults {
  static String handle(Throwable t) { return "throwable"; }
  static String handle(Object o) { return "object"; }

  static String viaIo(IOException e) { return handle(e); }
  static String viaAppError(AppError e) { return handle(e); }
}
"""

# Greptile on #2800: a cast to a generic array type kept no array dimension.
ARRAY_CAST = """package com.acme;

import java.util.List;

public class ArrayCast {
  static String m(List<String> l) { return "list"; }
  static String m(Object[] a) { return "array"; }

  static String viaGenericArrayCast(Object value) { return m((List<String>[]) value); }
}
"""

# Greptile on #2800: parameter types that share a simple name across a
# hierarchy are different types, so neither method overrides the other.
LIST_BASE = """package com.acme;

public class ListBase {
  public String m(java.util.List<String> l) { return "util"; }
}
"""

LIST_CHILD = """package com.acme;

public class ListChild extends ListBase {
  public String m(java.awt.List l) { return "awt"; }

  public String viaUtil(java.util.List<String> l) { return m(l); }
}
"""

# square/javapoet's shape: TypeName.java:360 and ArrayTypeName.java:103.
TYPE_NAME = """package com.acme;

import java.lang.reflect.GenericArrayType;
import java.lang.reflect.Type;
import java.util.LinkedHashMap;
import java.util.Map;

public class TypeName {
  public static TypeName get(Type type) { return get(type, new LinkedHashMap<>()); }

  static TypeName get(Type type, Map<Type, TypeName> map) {
    if (type instanceof GenericArrayType) {
      return ArrayTypeName.get((GenericArrayType) type, map);
    }
    return null;
  }
}
"""

ARRAY_TYPE_NAME = """package com.acme;

import java.lang.reflect.GenericArrayType;
import java.lang.reflect.Type;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.lang.model.type.ArrayType;

public final class ArrayTypeName extends TypeName {
  static ArrayTypeName get(ArrayType mirror, Map<Object, TypeName> typeVariables) {
    return null;
  }

  public static ArrayTypeName get(GenericArrayType type) {
    return get(type, new LinkedHashMap<>());
  }

  static ArrayTypeName get(GenericArrayType type, Map<Type, TypeName> map) {
    return null;
  }
}
"""

# Two overloads of one class whose parameter types share a simple name: one
# cannot override the other, so both stay candidates.
TWINS = """package com.acme;

public class Twins {
  static String of(javax.lang.model.type.TypeVariable t, int n) { return "model"; }
  static String of(java.lang.reflect.TypeVariable<?> t, int n) { return "reflect"; }

  static String viaReflect(java.lang.reflect.TypeVariable<?> t) { return of(t, 1); }
}
"""

# A varargs parameter handed straight on: square/javapoet's
# ParameterSpec.builder(..., Modifier... modifiers).
VARARGS = """package com.acme;

import java.util.Set;

public class Varargs {
  static String mods(String... names) { return "array"; }
  static String mods(Iterable<String> names) { return "iterable"; }

  static String forward(String... names) { return mods(names); }
  static String viaSet(Set<String> names) { return mods(names); }

  static String put(char c) { return "char"; }
  static String put(CharSequence s) { return "chars"; }

  static String viaBuilder(StringBuilder b) { return put(b); }
}
"""

# Greptile on #2800: the same pair written with imports, and a single
# same-named overload beside Object, where javac takes Object.
IMP_BASE = """package com.acme;

import java.util.List;

public class ImpBase {
  public String m(List<String> l) { return "util"; }
}
"""

IMP_CHILD = """package com.acme;

import java.awt.List;

public class ImpChild extends ImpBase {
  public String m(List l) { return "awt"; }
}
"""

IMP_USER = """package com.acme;

import java.util.List;

public class ImpUser {
  String viaChild(ImpChild c, List<String> l) { return c.m(l); }
}
"""

ONE_AWT = """package com.acme;

public class OneAwt {
  static String m(java.awt.List l) { return "awt"; }
  static String m(Object o) { return "object"; }

  static String viaUtil(java.util.List<String> l) { return m(l); }
}
"""

# Greptile on #2800: a class outside the index (org.vendor) may implement
# an interface the project declares, unlike a JDK class.
PLUGIN = """package com.acme;

public interface Plugin {}
"""

PLUGINS = """package com.acme;

import org.vendor.VendorPlugin;

public class Plugins {
  static String handle(Plugin p) { return "plugin"; }
  static String handle(Object o) { return "object"; }

  static String viaVendor(VendorPlugin p) { return handle(p); }
}
"""

# Greptile on #2800: a type variable stands for a reference type, so `T[]`
# takes no `int[]`, while `int[][]` is a `T[]` with T = int[].
GENERIC_ARRAYS = """package com.acme;

public class GenericArrays {
  static <T> String f(T[] a) { return "t"; }
  static String f(Object o) { return "o"; }

  static String viaInts(int[] xs) { return f(xs); }
  static String viaIntegers(Integer[] xs) { return f(xs); }
  static String viaIntGrid(int[][] xs) { return f(xs); }
}
"""

FILES = {
    "Names.java": NAMES,
    "Ext.java": EXT,
    "Hier.java": HIER,
    "Base3.java": BASE3,
    "Canvas.java": CANVAS,
    "FancyPainter.java": OVERRIDE,
    "Specific.java": SPECIFIC,
    "Iface.java": IFACE,
    "Prim.java": PRIM,
    "Proven.java": PROVEN,
    "TypeName.java": TYPE_NAME,
    "ArrayTypeName.java": ARRAY_TYPE_NAME,
    "Twins.java": TWINS,
    "Varargs.java": VARARGS,
    "Generic.java": GENERIC,
    "Faults.java": FAULTS,
    "ArrayCast.java": ARRAY_CAST,
    "ListBase.java": LIST_BASE,
    "ListChild.java": LIST_CHILD,
    "ImpBase.java": IMP_BASE,
    "ImpChild.java": IMP_CHILD,
    "ImpUser.java": IMP_USER,
    "OneAwt.java": ONE_AWT,
    "Plugin.java": PLUGIN,
    "Plugins.java": PLUGINS,
    "GenericArrays.java": GENERIC_ARRAYS,
}

ACME = f"{PROJECT}.com.acme"
GET_GENERIC_ARRAY = (
    "ArrayTypeName.ArrayTypeName.get(GenericArrayType,Map<Type, TypeName>)"
)


def _write(root: Path) -> Path:
    package = root / "com" / "acme"
    package.mkdir(parents=True)
    for name, body in FILES.items():
        (package / name).write_text(body, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    # caller suffix (`Names.viaCast(Shape)`) -> {callee suffix: resolution}.
    root = _write(tmp_path_factory.mktemp("jover") / PROJECT)
    ingestor = MagicMock()
    create_and_run_updater(root, ingestor, skip_if_missing="java")
    edges: dict[str, dict[str, str]] = {}
    for c in ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS:
            continue
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        caller = str(c.args[0][2]).removeprefix(f"{ACME}.")
        callee = str(c.args[2][2]).removeprefix(f"{ACME}.")
        edges.setdefault(caller, {})[callee] = str(props.get(cs.KEY_RESOLUTION))
    return edges


def _callees(calls: dict[str, dict[str, str]], caller: str) -> dict[str, str]:
    return calls.get(caller, {})


# --- the bug: the issue's three shapes pick the applicable overload ----------


def test_cast_argument_selects_the_cast_type_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Names.Names.viaCast(Shape)") == {
        "Names.Names.name(Square,int)": cs.EdgeResolution.EXACT
    }


def test_parenthesised_cast_argument_selects_the_cast_type_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Names.Names.viaParenCast(Shape)") == {
        "Names.Names.name(Square,int)": cs.EdgeResolution.EXACT
    }


def test_local_typed_by_a_cast_selects_the_cast_type_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Names.Names.viaVarCast(Shape)") == {
        "Names.Names.name(Square,int)": cs.EdgeResolution.EXACT
    }


def test_primitive_cast_argument_selects_the_primitive_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Prim.Prim.viaPrimitiveCast(double)") == {
        "Prim.Prim.take(int)": cs.EdgeResolution.EXACT
    }


def test_library_subtype_argument_keeps_the_other_arguments_deciding(
    calls: dict[str, dict[str, str]],
) -> None:
    # `new LinkedHashMap<>()` for a `Map` parameter used to rule out both
    # candidates, discarding the `Set` argument that tells them apart.
    assert _callees(calls, "Ext.Ext.viaParamExt(Set<String>)") == {
        "Ext.Ext.get(Set<String>,Map<Integer, String>)": cs.EdgeResolution.EXACT
    }


def test_library_subtype_beats_object(calls: dict[str, dict[str, str]]) -> None:
    assert _callees(calls, "Ext.Ext.viaLibrarySubtype()") == {
        "Ext.Ext.put(Map<String, Integer>)": cs.EdgeResolution.EXACT
    }


def test_inherited_overload_that_applies_beats_the_receivers_own(
    calls: dict[str, dict[str, str]],
) -> None:
    # Only Base2.conv(Shape,int) accepts a Square; Hier.conv(Circle,int) does
    # not, and a same-named method of an unrelated class is no candidate.
    assert _callees(calls, "Hier.Hier.use(Square)") == {
        "Hier.Base2.conv(Shape,int)": cs.EdgeResolution.EXACT
    }


def test_inherited_overload_in_another_file_is_weighed(
    calls: dict[str, dict[str, str]],
) -> None:
    expected = {"Base3.Base3.draw(Shape)": cs.EdgeResolution.EXACT}
    assert _callees(calls, "Canvas.Canvas.paint(Square)") == expected
    assert _callees(calls, "Canvas.Canvas.paintOn(Canvas,Square)") == expected


def test_project_superclass_parameter_beats_object(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Specific.Specific.viaSubclass(Square)") == {
        "Specific.Specific.take(Shape)": cs.EdgeResolution.EXACT
    }


def test_nearest_superclass_parameter_wins(
    calls: dict[str, dict[str, str]],
) -> None:
    # Tile extends Square extends Shape: fit(Square) is the more specific
    # overload, though fit(Shape) is declared first.
    assert _callees(calls, "Specific.Specific.viaNearest(Tile)") == {
        "Specific.Specific.fit(Square)": cs.EdgeResolution.EXACT
    }


def test_project_interface_parameter_selects_the_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Iface.Iface.viaImpl(Impl)") == {
        "Iface.Iface.accept(Api)": cs.EdgeResolution.EXACT
    }


def test_javapoet_cast_call_binds_the_generic_array_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    caller = "TypeName.TypeName.get(Type,Map<Type, TypeName>)"
    assert _callees(calls, caller) == {GET_GENERIC_ARRAY: cs.EdgeResolution.EXACT}


def test_javapoet_diamond_call_binds_the_generic_array_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    caller = "ArrayTypeName.ArrayTypeName.get(GenericArrayType)"
    assert _callees(calls, caller) == {GET_GENERIC_ARRAY: cs.EdgeResolution.EXACT}


def test_undecidable_pick_links_every_tied_overload_as_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    # `s.get()` returns a type the parser cannot see, so nothing tells
    # name(Circle,int) from name(Square,int): declaration order is no answer.
    assert _callees(calls, "Names.Names.viaUnknown(Supplier<Square>)") == {
        "Names.Names.name(Circle,int)": cs.EdgeResolution.OVERLOAD,
        "Names.Names.name(Square,int)": cs.EdgeResolution.OVERLOAD,
    }


def test_array_argument_selects_the_varargs_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Varargs.Varargs.forward(String...)") == {
        "Varargs.Varargs.mods(String...)": cs.EdgeResolution.EXACT
    }


def test_same_class_overloads_sharing_a_simple_name_are_told_apart(
    calls: dict[str, dict[str, str]],
) -> None:
    # Both parameters read `TypeVariable` once the package is dropped; the
    # package the argument's type names decides, as javac's does.
    assert _callees(
        calls, "Twins.Twins.viaReflect(java.lang.reflect.TypeVariable<?>)"
    ) == {
        "Twins.Twins.of(java.lang.reflect.TypeVariable<?>,int)": cs.EdgeResolution.EXACT
    }


def test_type_variable_overload_with_an_unknown_argument_ties(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Generic.Generic.viaUnknown(Supplier<String>)") == {
        "Generic.Generic.f(T)": cs.EdgeResolution.OVERLOAD,
        "Generic.Generic.f(String)": cs.EdgeResolution.OVERLOAD,
    }


def test_bounded_type_variable_overload_is_no_loss_to_object(
    calls: dict[str, dict[str, str]],
) -> None:
    # javac picks g(T): T is bounded by CharSequence, which a String is.
    assert _callees(calls, "Generic.Generic.viaBounded(String)") == {
        "Generic.Generic.g(T)": cs.EdgeResolution.OVERLOAD,
        "Generic.Generic.g(Object)": cs.EdgeResolution.OVERLOAD,
    }


def test_class_type_variable_overload_is_no_loss_to_object(
    calls: dict[str, dict[str, str]],
) -> None:
    # javac picks put(T): the receiver is a Box<Integer>.
    assert _callees(calls, "Generic.BoxUser.viaInteger(Box<Integer>,Integer)") == {
        "Generic.Box.put(T)": cs.EdgeResolution.OVERLOAD,
        "Generic.Box.put(Object)": cs.EdgeResolution.OVERLOAD,
    }


@pytest.mark.parametrize(
    "caller",
    ["Faults.Faults.viaIo(IOException)", "Faults.Faults.viaAppError(AppError)"],
)
def test_unindexed_jdk_supertype_is_no_loss_to_object(
    calls: dict[str, dict[str, str]], caller: str
) -> None:
    # javac picks handle(Throwable); nothing indexed shows that IOException,
    # or Exception above AppError, is a Throwable.
    assert _callees(calls, caller) == {
        "Faults.Faults.handle(Throwable)": cs.EdgeResolution.OVERLOAD,
        "Faults.Faults.handle(Object)": cs.EdgeResolution.OVERLOAD,
    }


def test_unindexed_jdk_supertype_ties_with_an_inherited_object_overload(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Faults.FaultChild.viaIo(IOException)") == {
        "Faults.FaultChild.report(Throwable)": cs.EdgeResolution.OVERLOAD,
        "Faults.FaultBase.report(Object)": cs.EdgeResolution.OVERLOAD,
    }


def test_unindexed_jdk_interface_is_no_loss_to_object(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Proven.Proven.viaUnindexedJdkSupertype(Instant)") == {
        "Proven.Proven.keep(Temporal)": cs.EdgeResolution.OVERLOAD,
        "Proven.Proven.keep(Object)": cs.EdgeResolution.OVERLOAD,
    }


def test_generic_array_cast_keeps_its_array_type(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "ArrayCast.ArrayCast.viaGenericArrayCast(Object)") == {
        "ArrayCast.ArrayCast.m(Object[])": cs.EdgeResolution.EXACT
    }


def test_same_simple_name_parameter_in_a_subclass_is_no_override(
    calls: dict[str, dict[str, str]],
) -> None:
    # java.awt.List is not java.util.List: ListBase.m is inherited, not
    # overridden, and it is the one a java.util.List argument fits.
    assert _callees(calls, "ListChild.ListChild.viaUtil(java.util.List<String>)") == {
        "ListBase.ListBase.m(java.util.List<String>)": cs.EdgeResolution.EXACT
    }


def test_same_simple_name_parameter_through_imports_is_told_apart(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "ImpUser.ImpUser.viaChild(ImpChild,List<String>)") == {
        "ImpBase.ImpBase.m(List<String>)": cs.EdgeResolution.EXACT
    }


def test_same_simple_name_of_another_type_is_no_exact_match(
    calls: dict[str, dict[str, str]],
) -> None:
    # javac takes m(Object); a java.util.List is no java.awt.List, though
    # nothing indexed proves it.
    callees = _callees(calls, "OneAwt.OneAwt.viaUtil(java.util.List<String>)")
    assert callees.get("OneAwt.OneAwt.m(Object)") is not None
    assert cs.EdgeResolution.EXACT not in callees.values()


def test_class_outside_the_index_may_implement_a_project_interface(
    calls: dict[str, dict[str, str]],
) -> None:
    # javac picks handle(Plugin): VendorPlugin implements it. Only a JDK
    # class is known never to.
    assert _callees(calls, "Plugins.Plugins.viaVendor(VendorPlugin)") == {
        "Plugins.Plugins.handle(Plugin)": cs.EdgeResolution.OVERLOAD,
        "Plugins.Plugins.handle(Object)": cs.EdgeResolution.OVERLOAD,
    }


def test_primitive_array_never_fits_a_type_variable_array(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "GenericArrays.GenericArrays.viaInts(int[])") == {
        "GenericArrays.GenericArrays.f(Object)": cs.EdgeResolution.EXACT
    }


@pytest.mark.parametrize(
    "caller",
    [
        "GenericArrays.GenericArrays.viaIntegers(Integer[])",
        "GenericArrays.GenericArrays.viaIntGrid(int[][])",
    ],
)
def test_reference_array_still_fits_a_type_variable_array(
    calls: dict[str, dict[str, str]], caller: str
) -> None:
    # javac picks f(T[]) for both: T is Integer, or int[].
    assert _callees(calls, caller) == {
        "GenericArrays.GenericArrays.f(T[])": cs.EdgeResolution.OVERLOAD,
        "GenericArrays.GenericArrays.f(Object)": cs.EdgeResolution.OVERLOAD,
    }


# --- negative: what already resolved, or must not, stays as it was ----------


def test_exact_parameter_type_still_binds_alone(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Names.Names.viaParam(Square)") == {
        "Names.Names.name(Square,int)": cs.EdgeResolution.EXACT
    }
    assert _callees(
        calls, "Ext.Ext.viaParamExt2(Set<String>,Map<Integer, String>)"
    ) == {"Ext.Ext.get(Set<String>,Map<Integer, String>)": cs.EdgeResolution.EXACT}


def test_receivers_own_exact_overload_still_wins(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Hier.Hier.useCircle(Circle)") == {
        "Hier.Hier.conv(Circle,int)": cs.EdgeResolution.EXACT
    }


def test_override_is_called_not_the_method_it_overrides(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "FancyPainter.FancyPainter.run(Square)") == {
        "FancyPainter.FancyPainter.paint(Shape)": cs.EdgeResolution.EXACT
    }


def test_unrelated_class_overload_is_not_a_candidate(
    calls: dict[str, dict[str, str]],
) -> None:
    callees = {callee for edges in calls.values() for callee in edges}
    assert "Hier.Stranger.conv(Square,int)" not in callees


def test_a_jdk_argument_never_reaches_a_project_parameter(
    calls: dict[str, dict[str, str]],
) -> None:
    # A JDK class cannot extend a class this project declares, so
    # hold(Widget) cannot apply to an Instant and hold(Object) is certain.
    assert _callees(calls, "Proven.Proven.viaUnrelated(Instant)") == {
        "Proven.Proven.hold(Object)": cs.EdgeResolution.EXACT
    }


def test_exact_overload_still_beats_a_type_variable_one(
    calls: dict[str, dict[str, str]],
) -> None:
    # javac prefers f(String) over <T> f(T) for a String: it is more specific.
    assert _callees(calls, "Generic.Generic.viaString(String)") == {
        "Generic.Generic.f(String)": cs.EdgeResolution.EXACT
    }


def test_type_variable_parameter_keeps_its_candidate_applicable(
    calls: dict[str, dict[str, str]],
) -> None:
    # `"x"` fits `T` in both overloads; only the `1` decides, for put(int,T).
    assert _callees(calls, "Generic.Generic.viaTypeVariableArgument()") == {
        "Generic.Generic.put(int,T)": cs.EdgeResolution.EXACT
    }


def test_variable_arity_call_never_displaces_a_proven_pick(
    calls: dict[str, dict[str, str]],
) -> None:
    # Whether a Set is a String is not proven, but mods(String...) would need
    # a variable-arity call, which javac weighs only when nothing else applies.
    assert _callees(calls, "Varargs.Varargs.viaSet(Set<String>)") == {
        "Varargs.Varargs.mods(Iterable<String>)": cs.EdgeResolution.EXACT
    }


def test_a_reference_argument_never_reaches_a_primitive_parameter(
    calls: dict[str, dict[str, str]],
) -> None:
    assert _callees(calls, "Varargs.Varargs.viaBuilder(StringBuilder)") == {
        "Varargs.Varargs.put(CharSequence)": cs.EdgeResolution.EXACT
    }


# --- the ranking itself -------------------------------------------------------


def test_library_supertype_is_ranked_by_its_distance() -> None:
    depths = {"HashMap": 1, "AbstractMap": 2, "Cloneable": 2, "Map": 3}
    assert _overload_rank(
        "C.put(Map<String, Integer>)",
        ("LinkedHashMap<>",),
        (JavaSupertypes(depths, frozenset()),),
    ) == JavaOverloadRank(unproven=0, conversions=cs.JAVA_RANK_SUPERTYPE, distance=3)


def test_a_parameter_a_whole_hierarchy_misses_is_ruled_out() -> None:
    walked = JavaSupertypes({"Shape": 1}, frozenset({"Widget"}))
    assert _overload_rank("C.take(Widget)", ("Square",), (walked,)) is None


@pytest.mark.parametrize(
    ("param", "expected"),
    [
        ("Shape[]", JavaOverloadRank(0, cs.JAVA_RANK_SUPERTYPE, 1)),
        ("Object", JavaOverloadRank(0, cs.JAVA_RANK_OBJECT, 0)),
        ("Shape", None),
    ],
)
def test_an_array_argument_widens_only_as_an_array(
    param: str, expected: JavaOverloadRank | None
) -> None:
    walked = JavaSupertypes({"Shape": 1}, frozenset())
    assert _overload_rank(f"C.take({param})", ("Square[]",), (walked,)) == expected


def test_primitive_array_elements_neither_widen_nor_box() -> None:
    assert _overload_rank("C.take(long[])", ("int[]",)) is None
    assert _overload_rank("C.take(Integer[])", ("int[]",)) is None


def test_a_conversion_through_an_unseen_hierarchy_stays_possible() -> None:
    # Nothing indexed says a Gadget is not a Widget, so the candidate is
    # weighed last instead of being dropped.
    assert _overload_rank("C.take(Widget)", ("Gadget",)) == JavaOverloadRank(
        unproven=1, conversions=0, distance=0
    )


@pytest.mark.parametrize("arg_type", ["int", "Integer", "String", "Object"])
def test_types_with_fixed_supertypes_still_rule_out_other_parameters(
    arg_type: str,
) -> None:
    assert _overload_rank("C.take(Widget)", (arg_type,)) is None


def test_generic_array_type_keeps_its_dimensions() -> None:
    assert _java_param_type_names("C.m(List<String>[],java.util.Map<K, V>[][])") == [
        "List[]",
        "Map[][]",
    ]


def test_varargs_parameter_keeps_its_name() -> None:
    # The `...` is no package separator; read as one it left an empty name.
    assert _java_param_type_names("C.m(int,java.lang.String...)") == [
        "int",
        "String...",
    ]
