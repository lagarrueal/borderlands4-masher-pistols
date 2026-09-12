"""Fake unrealsdk / mods_base so jakobs_masher can be exercised off-game.

Models the parts of the engine the mod actually touches: objects with unreal
properties, an Outer chain, class default objects, find_all, weak pointers, and
the mods_base decorators.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

_NEXT_ADDR = [0x1000]

# The local player controller, as mods_base.get_pc() would return it.
PLAYER: dict[str, object] = {"pc": None}


def set_player(pc: object) -> None:
    PLAYER["pc"] = pc


_REGISTRY: dict[str, list["FakeObject"]] = {}


class FakeClass:
    def __init__(self, name: str) -> None:
        self.Name = name
        self.ClassDefaultObject: FakeObject | None = None


_CLASSES: dict[str, FakeClass] = {}


def get_class(name: str) -> FakeClass:
    if name not in _CLASSES:
        cls = FakeClass(name)
        _CLASSES[name] = cls
        cdo = FakeObject(name, path=f"Default__{name}", is_cdo=True)
        cls.ClassDefaultObject = cdo
    return _CLASSES[name]


class FakeObject:
    """An unreal object: arbitrary properties plus Outer/Class."""

    def __init__(
        self,
        class_name: str,
        path: str = "",
        outer: "FakeObject | None" = None,
        is_cdo: bool = False,
        **props: Any,
    ) -> None:
        object.__setattr__(self, "_props", dict(props))
        object.__setattr__(self, "_class_name", class_name)
        object.__setattr__(self, "_path", path or f"{class_name}_{_NEXT_ADDR[0]}")
        object.__setattr__(self, "_outer", outer)
        _NEXT_ADDR[0] += 0x40
        object.__setattr__(self, "_addr", _NEXT_ADDR[0])
        if not is_cdo:
            _REGISTRY.setdefault(class_name, []).append(self)

    # -- unreal-ish surface ------------------------------------------------ #

    @property
    def Class(self) -> FakeClass:  # noqa: N802
        return get_class(object.__getattribute__(self, "_class_name"))

    @property
    def Outer(self) -> "FakeObject | None":  # noqa: N802
        return object.__getattribute__(self, "_outer")

    def _get_address(self) -> int:
        return object.__getattribute__(self, "_addr")

    def _path_name(self) -> str:
        return object.__getattribute__(self, "_path")

    def __getattr__(self, name: str) -> Any:
        props = object.__getattribute__(self, "_props")
        if name in props:
            return props[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            # Test-harness internals, not unreal fields.
            object.__setattr__(self, name, value)
            return
        props = object.__getattribute__(self, "_props")
        if name not in props:
            # The engine refuses writes to properties that do not exist.
            raise AttributeError(f"no such property {name}")
        props[name] = value

    def __dir__(self) -> list[str]:
        return list(object.__getattribute__(self, "_props")) + ["Class", "Outer"]

    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)

    def __repr__(self) -> str:
        return f"{object.__getattribute__(self, '_class_name')} {self._path_name()}"


class BoundFunction:
    def __init__(self, fn: Any) -> None:
        self.fn = fn

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)


class WeakPointer:
    def __init__(self, obj: Any = None) -> None:
        self._obj = obj

    def __call__(self) -> Any:
        return self._obj

    def kill(self) -> None:
        self._obj = None


class WrappedStruct:
    """A Gbx attribute struct: fields, an address, but no path name.

    Modelled on what the game actually hands back - reading
    `behaviour.ProjectilesPerShot` yields one of these, not a number, which is
    what broke the first working build.
    """

    def __init__(self, **fields: Any) -> None:
        object.__setattr__(self, "_fields", dict(fields))
        _NEXT_ADDR[0] += 0x40
        object.__setattr__(self, "_addr", _NEXT_ADDR[0])

    def _get_address(self) -> int:
        return object.__getattribute__(self, "_addr")

    def __getattr__(self, name: str) -> Any:
        fields = object.__getattribute__(self, "_fields")
        if name in fields:
            return fields[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        fields = object.__getattribute__(self, "_fields")
        if name not in fields:
            raise AttributeError(f"no such struct field {name}")
        fields[name] = value

    def __dir__(self) -> list[str]:
        return list(object.__getattribute__(self, "_fields"))

    def __repr__(self) -> str:
        return f"WrappedStruct({object.__getattribute__(self, '_fields')})"


class UObject(FakeObject):  # type: ignore[misc]
    pass


def find_all(cls_name: str, exact: bool = True):
    yield from _REGISTRY.get(cls_name, [])
    cls = _CLASSES.get(cls_name)
    if cls is not None and cls.ClassDefaultObject is not None:
        yield cls.ClassDefaultObject


def reset_world() -> None:
    _REGISTRY.clear()
    PLAYER["pc"] = None


# --------------------------------------------------------------------------- #
# Module installation
# --------------------------------------------------------------------------- #


def install() -> None:
    unrealsdk = types.ModuleType("unrealsdk")
    unrealsdk.find_all = find_all
    unrealsdk.find_class = lambda name, fq=None: get_class(name)

    unreal = types.ModuleType("unrealsdk.unreal")
    unreal.BoundFunction = BoundFunction
    unreal.UObject = UObject
    unreal.WeakPointer = WeakPointer
    unreal.WrappedStruct = WrappedStruct
    unrealsdk.unreal = unreal

    hooks = types.ModuleType("unrealsdk.hooks")

    class Type(Enum):
        PRE = auto()
        POST = auto()
        POST_UNCONDITIONAL = auto()

    hooks.Type = Type
    unrealsdk.hooks = hooks

    sys.modules["unrealsdk"] = unrealsdk
    sys.modules["unrealsdk.unreal"] = unreal
    sys.modules["unrealsdk.hooks"] = hooks

    mods_base = types.ModuleType("mods_base")

    class CoopSupport(Enum):
        Unknown = auto()
        Incompatible = auto()
        RequiresAllPlayers = auto()
        ClientSide = auto()
        HostOnly = auto()

    @dataclass
    class _Option:
        identifier: str
        value: Any
        display_name: str | None = None
        description: str = ""

    def SliderOption(  # noqa: N802
        identifier, value, min_value, max_value, step=1, is_integer=True, **kw
    ):
        assert min_value <= value <= max_value, f"{identifier} default out of range"
        assert step <= (max_value - min_value), f"{identifier} step too large"
        if is_integer:
            for x in (value, min_value, max_value, step):
                assert x == int(x), f"{identifier} non-integer field with is_integer"
        return _Option(identifier, value, kw.get("display_name"), kw.get("description", ""))

    def BoolOption(identifier, value, true_text=None, false_text=None, **kw):  # noqa: N802
        return _Option(identifier, value, kw.get("display_name"), kw.get("description", ""))

    REGISTERED: dict[str, Any] = {
        "hooks": [],
        "commands": [],
        "keybinds": [],
        "mod": None,
    }

    class FakeHook:
        """Mirrors mods_base.HookType's surface: hook_funcs + get_active_count."""

        def __init__(self, fn, target, typ):
            self.__wrapped__ = fn
            self.hook_identifier = fn.__name__
            self.hook_funcs = [(target, typ)]
            self.active = True

        def get_active_count(self) -> int:
            return len(self.hook_funcs) if self.active else 0

        def __call__(self, *a, **kw):
            return self.__wrapped__(*a, **kw)

    def hook(target: str, typ: Any = None, identifier: str | None = None):
        def deco(fn):
            h = FakeHook(fn, target, typ)
            REGISTERED["hooks"].append(h)
            return h

        return deco

    class FakeKeybind:
        def __init__(self, fn, name, description=""):
            self.__wrapped__ = fn
            self.name = name
            self.description = description

        def __call__(self, *a, **kw):
            return self.__wrapped__(*a, **kw)

    def keybind(name: str, *a: Any, **kw: Any):
        def deco(fn):
            kb = FakeKeybind(fn, name, kw.get("description", ""))
            REGISTERED["keybinds"].append(kb)
            return kb

        return deco

    class ArgParseCommand:
        def __init__(self, fn, cmd, **kw):
            import argparse

            self.fn = fn
            self.cmd = cmd
            self.parser = argparse.ArgumentParser(prog=cmd, **kw)

        def add_argument(self, *a, **kw):
            return self.parser.add_argument(*a, **kw)

        def __call__(self, line: str = ""):
            ns = self.parser.parse_args(line.split())
            return self.fn(ns)

    def command(cmd=None, splitter=None, **kw):
        def deco(fn):
            c = ArgParseCommand(fn, cmd or fn.__name__, **kw)
            REGISTERED["commands"].append(c)
            return c

        return deco

    def build_mod(**kw):
        REGISTERED["mod"] = kw
        return kw

    def get_pc():
        return PLAYER["pc"]

    mods_base.BoolOption = BoolOption
    mods_base.SliderOption = SliderOption
    mods_base.CoopSupport = CoopSupport
    mods_base.build_mod = build_mod
    mods_base.command = command
    mods_base.hook = hook
    mods_base.keybind = keybind
    mods_base.get_pc = get_pc
    mods_base.REGISTERED = REGISTERED
    sys.modules["mods_base"] = mods_base
