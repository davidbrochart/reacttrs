# this is a partial copy/paste from:
# https://github.com/Textualize/textual/blob/373fc95fc1a5650fd864053e4153eb2d6b38f087/src/textual/reactive.py

from __future__ import annotations
import asyncio
from inspect import isawaitable
from typing import Any, Awaitable, Callable, ClassVar, Generic, TypeVar

from ._callback import count_parameters


Reactable = Any
ReactiveType = TypeVar("ReactiveType")
background_tasks = set()


class Reactive(Generic[ReactiveType]):

    _reactives: ClassVar[dict[str, object]] = {}

    def __init__(
        self,
        default: ReactiveType | Callable[[], ReactiveType],
        init: bool = False,
        always_update: bool = False,
        compute: bool = True,
        on_set: Callable[[Reactable, str, ReactiveType], None] | None = None,
    ) -> None:
        self._default = default
        self._init = init
        self._always_update = always_update
        self._run_compute = compute
        self._on_set = on_set

    def _initialize_reactive(self, obj: Reactable, name: str) -> None:
        """Initialized a reactive attribute on an object.

        Args:
            obj: An object with reactive attributes.
            name: Name of attribute.
        """
        internal_name = f"_reactive_{name}"
        if hasattr(obj, internal_name):
            # Attribute already has a value
            return

        compute_method = getattr(obj, f"compute_{name}", None)
        if compute_method is not None and self._init:
            default = getattr(obj, f"compute_{name}")()
        else:
            default_or_callable = self._default
            default = (
                default_or_callable()
                if callable(default_or_callable)
                else default_or_callable
            )
        setattr(obj, internal_name, default)
        if self._init:
            self._check_watchers(obj, name, default)

    @classmethod
    def _initialize_object(cls, obj: Reactable) -> None:
        """Set defaults and call any watchers / computes for the first time.

        Args:
            obj: An object with Reactive descriptors
        """
        for name, reactive in obj._reactives.items():
            reactive._initialize_reactive(obj, name)

    @classmethod
    def _reset_object(cls, obj: object) -> None:
        """Reset reactive structures on object (to avoid reference cycles).

        Args:
            obj: A reactive object.
        """
        getattr(obj, "__watchers", {}).clear()
        getattr(obj, "__computes", []).clear()

    #def __set_name__(self, owner: Type[MessageTarget], name: str) -> None:
    def __set_name__(self, owner: Any, name: str) -> None:
        # Check for compute method
        if hasattr(owner, f"compute_{name}"):
            # Compute methods are stored in a list called `__computes`
            try:
                computes = getattr(owner, "__computes")
            except AttributeError:
                computes = []
                setattr(owner, "__computes", computes)
            computes.append(name)

        # The name of the attribute
        self.name = name
        # The internal name where the attribute's value is stored
        self.internal_name = f"_reactive_{name}"
        self.compute_name = f"compute_{name}"
        default = self._default
        setattr(owner, f"_default_{name}", default)

    def __get__(self, obj: Reactable, obj_type: type[object]) -> ReactiveType:
        internal_name = self.internal_name
        if not hasattr(obj, internal_name):
            self._initialize_reactive(obj, self.name)

        if hasattr(obj, self.compute_name):
            value: ReactiveType
            old_value = getattr(obj, internal_name)
            value = getattr(obj, self.compute_name)()
            setattr(obj, internal_name, value)
            self._check_watchers(obj, self.name, old_value)
            return value
        else:
            return getattr(obj, internal_name)

    def __set__(self, obj: Reactable, value: ReactiveType) -> None:
        self._initialize_reactive(obj, self.name)
        name = self.name
        current_value = getattr(obj, name)
        # Check for validate function
        validate_function = getattr(obj, f"validate_{name}", None)
        # Call validate
        if callable(validate_function):
            value = validate_function(value)
        # If the value has changed, or this is the first time setting the value
        if current_value != value or self._always_update:
            # Store the internal value
            setattr(obj, self.internal_name, value)

            # Check all watchers
            self._check_watchers(obj, name, current_value)

            if self._run_compute:
                self._compute(obj)

            # Call side effect
            if callable(self._on_set):
                self._on_set(obj, self.name, value)

    @classmethod
    def _check_watchers(cls, obj: Reactable, name: str, old_value: Any):
        """Check watchers, and call watch methods / computes

        Args:
            obj: The reactable object.
            name: Attribute name.
            old_value: The old (previous) value of the attribute.
        """
        # Get the current value.
        internal_name = f"_reactive_{name}"
        value = getattr(obj, internal_name)

        async def await_watcher(awaitable: Awaitable) -> None:
            """Coroutine to await an awaitable returned from a watcher"""
            await awaitable
            # Watcher may have changed the state, so run compute again
            Reactive._compute(obj)

        def invoke_watcher(
            watch_function: Callable, old_value: object, value: object
        ) -> None:
            """Invoke a watch function.

            Args:
                watch_function: A watch function, which may be sync or async.
                old_value: The old value of the attribute.
                value: The new value of the attribute.

            """
            param_count = count_parameters(watch_function)
            if param_count == 2:
                watch_result = watch_function(old_value, value)
            elif param_count == 1:
                watch_result = watch_function(value)
            else:
                watch_result = watch_function()
            if isawaitable(watch_result):
                # Result is awaitable, so we need to await it within an async context
                task = asyncio.create_task(await_watcher(watch_result))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)

        watch_function = getattr(obj, f"watch_{name}", None)
        if callable(watch_function):
            invoke_watcher(watch_function, old_value, value)

        # Process "global" watchers
        watchers: list[tuple[Reactable, Callable]]
        watchers = getattr(obj, "__watchers", {}).get(name, [])
        # Remove any watchers for reactables that have since closed
        if watchers:
            watchers[:] = [
                (reactable, callback)
                for reactable, callback in watchers
                if reactable.is_attached and not reactable._closing
            ]
            for reactable, callback in watchers:
                with reactable.prevent(*obj._prevent_message_types_stack[-1]):
                    invoke_watcher(callback, old_value, value)

    @classmethod
    def _compute(cls, obj: Reactable) -> None:
        """Invoke all computes.

        Args:
            obj: Reactable object.
        """
        if not hasattr(obj, "_reactives"):
            return

        for compute in obj._reactives.keys():
            try:
                compute_method = getattr(obj, f"compute_{compute}")
            except AttributeError:
                continue
            current_value = getattr(obj, f"_reactive_{compute}")
            value = compute_method()
            setattr(obj, f"_reactive_{compute}", value)
            if value != current_value:
                cls._check_watchers(obj, compute, current_value)

class reactive(Reactive[ReactiveType]):
    """Create a reactive attribute.

    Args:
        default: A default value or callable that returns a default.
        layout: Perform a layout on change. Defaults to False.
        repaint: Perform a repaint on change. Defaults to True.
        init: Call watchers on initialize (post mount). Defaults to True.
        always_update: Call watchers even when the new value equals the old value. Defaults to False.
    """

    def __init__(
        self,
        default: ReactiveType | Callable[[], ReactiveType],
        *,
        init: bool = True,
        always_update: bool = False,
        on_set: Callable[[Reactable, str, ReactiveType], None] | None = None,
    ) -> None:
        super().__init__(
            default,
            init=init,
            always_update=always_update,
            on_set=on_set,
        )
