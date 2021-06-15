# -*- encoding: utf-8 -*-
#
# Copyright © 2018-2020 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
from collections import abc
import dataclasses
import datetime
import inspect
import operator
import re
import typing

from mergify_engine import date
from mergify_engine import utils


_T = typing.TypeVar("_T")


class InvalidQuery(Exception):
    pass


class ParseError(InvalidQuery):
    def __init__(self, tree: "TreeT") -> None:
        super().__init__(f"Unable to parse tree: {str(tree)}")
        self.tree = tree


class UnknownAttribute(InvalidQuery, ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"Unknown attribute: {str(key)}")
        self.key = key


class UnknownOperator(InvalidQuery, ValueError):
    def __init__(self, operator: str) -> None:
        super().__init__(f"Unknown operator: {str(operator)}")
        self.operator = operator


class InvalidOperator(InvalidQuery, TypeError):
    def __init__(self, operator: str) -> None:
        super().__init__(f"Invalid operator: {str(operator)}")
        self.operator = operator


class InvalidArguments(InvalidQuery, ValueError):
    def __init__(self, arguments: typing.Any) -> None:
        super().__init__(f"Invalid arguments: {str(arguments)}")
        self.arguments = arguments


def _identity(value: _T) -> _T:
    return value


TreeBinaryLeafT = typing.Tuple[str, typing.Any]

TreeT = typing.TypedDict(
    "TreeT",
    {
        # mypy does not support recursive definition yet
        "-": "TreeT",  # type: ignore[misc]
        "=": TreeBinaryLeafT,
        "<": TreeBinaryLeafT,
        ">": TreeBinaryLeafT,
        "<=": TreeBinaryLeafT,
        ">=": TreeBinaryLeafT,
        "!=": TreeBinaryLeafT,
        "~=": TreeBinaryLeafT,
        "@": typing.Union["TreeT", "CompiledTreeT[GetAttrObject]"],  # type: ignore[misc]
        "or": typing.Iterable[typing.Union["TreeT", "CompiledTreeT[GetAttrObject]"]],  # type: ignore[misc]
        "and": typing.Iterable[typing.Union["TreeT", "CompiledTreeT[GetAttrObject]"]],  # type: ignore[misc]
    },
    total=False,
)


class GetAttrObject(typing.Protocol):
    def __getattribute__(self, key: typing.Any) -> typing.Any:
        ...


GetAttrObjectT = typing.TypeVar("GetAttrObjectT", bound=GetAttrObject)
FilterResultT = typing.TypeVar("FilterResultT")
CompiledTreeT = typing.Callable[[GetAttrObjectT], typing.Awaitable[FilterResultT]]


UnaryOperatorT = typing.Callable[[typing.Any], FilterResultT]
BinaryOperatorT = typing.Tuple[
    typing.Callable[[typing.Any, typing.Any], FilterResultT],
    typing.Callable[[typing.Iterable[object]], FilterResultT],
    typing.Callable[[typing.Any], typing.Any],
]
MultipleOperatorT = typing.Callable[..., FilterResultT]


@dataclasses.dataclass(repr=False)
class Filter(typing.Generic[FilterResultT]):
    tree: typing.Union[TreeT, CompiledTreeT[GetAttrObject, FilterResultT]]
    unary_operators: typing.Dict[str, UnaryOperatorT[FilterResultT]]
    binary_operators: typing.Dict[str, BinaryOperatorT[FilterResultT]]
    multiple_operators: typing.Dict[str, MultipleOperatorT[FilterResultT]]

    value_expanders: typing.Dict[
        str, typing.Callable[[typing.Any], typing.List[typing.Any]]
    ] = dataclasses.field(default_factory=dict, init=False)

    _eval: CompiledTreeT[GetAttrObject, FilterResultT] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        self._eval = self.build_evaluator(self.tree)

    def __str__(self):
        return self._tree_to_str(self.tree)

    def _tree_to_str(self, tree):
        # We don't do any kind of validation here since build_evaluator does
        # that.
        op, nodes = list(tree.items())[0]

        if op in self.multiple_operators:
            return "(" + f" {op} ".join(self._tree_to_str(n) for n in nodes) + ")"
        if op in self.unary_operators:
            return op + self._tree_to_str(nodes)
        if op in self.binary_operators:
            if isinstance(nodes[1], bool):
                if self.binary_operators[op][0] != operator.eq:
                    raise InvalidOperator(op)
                return ("" if nodes[1] else "-") + str(nodes[0])
            elif isinstance(nodes[1], datetime.datetime):
                return (
                    str(nodes[0])
                    + op
                    + nodes[1].replace(tzinfo=None).isoformat(timespec="seconds")
                )
            elif isinstance(nodes[1], datetime.time):
                return (
                    str(nodes[0])
                    + op
                    + nodes[1].replace(tzinfo=None).isoformat(timespec="minutes")
                )
            else:
                return str(nodes[0]) + op + str(nodes[1])
        raise InvalidOperator(op)  # pragma: no cover

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}({str(self)})"

    async def __call__(self, obj: GetAttrObjectT) -> FilterResultT:
        return await self._eval(obj)

    LENGTH_OPERATOR = "#"

    @staticmethod
    def _to_list(item: typing.Union[_T, typing.Iterable[_T]]) -> typing.List[_T]:
        if isinstance(item, str):
            return [typing.cast(_T, item)]

        if isinstance(item, abc.Iterable):
            return list(item)

        return [item]

    async def _get_attribute_values(
        self,
        obj: GetAttrObjectT,
        attribute_name: str,
    ) -> typing.List[typing.Any]:
        op: typing.Callable[[typing.Any], typing.Any]
        if attribute_name.startswith(self.LENGTH_OPERATOR):
            attribute_name = attribute_name[1:]
            op = len
        else:
            attribute_name = attribute_name
            op = _identity
        try:
            attr = getattr(obj, attribute_name)
            if inspect.iscoroutine(attr):
                attr = await attr
        except KeyError:
            raise UnknownAttribute(attribute_name)
        try:
            values = op(attr)
        except TypeError:
            raise InvalidOperator(attribute_name)

        return self._to_list(values)

    def build_evaluator(
        self,
        tree: typing.Union[TreeT, CompiledTreeT[GetAttrObject, FilterResultT]],
    ) -> CompiledTreeT[GetAttrObject, FilterResultT]:
        if callable(tree):
            return tree

        if len(tree) != 1:
            raise ParseError(tree)

        operator_name, nodes = list(tree.items())[0]

        if operator_name == "@":
            # NOTE(sileht): the value is already a TreeT, so just evaluate it.
            # e.g., {"@", ("schedule", {"and": [{"=", ("time", "10:10"), ...}]})}
            return self.build_evaluator(typing.cast(typing.Tuple[str, TreeT], nodes)[1])

        try:
            multiple_op = self.multiple_operators[operator_name]
        except KeyError:
            try:
                unary_operator = self.unary_operators[operator_name]
            except KeyError:
                try:
                    binary_operator = self.binary_operators[operator_name]
                except KeyError:
                    raise UnknownOperator(operator_name)
                nodes = typing.cast(TreeBinaryLeafT, nodes)
                return self._handle_binary_op(binary_operator, nodes)
            nodes = typing.cast(TreeT, nodes)
            return self._handle_unary_op(unary_operator, nodes)
        if not isinstance(nodes, abc.Iterable):
            raise InvalidArguments(nodes)
        return self._handle_multiple_op(multiple_op, nodes)

    def _handle_binary_op(
        self,
        op: BinaryOperatorT[FilterResultT],
        nodes: TreeBinaryLeafT,
    ) -> CompiledTreeT[GetAttrObject, FilterResultT]:
        if len(nodes) != 2:
            raise InvalidArguments(nodes)

        binary_op, iterable_op, compile_fn = op
        try:
            attribute_name, reference_value = (nodes[0], compile_fn(nodes[1]))
        except Exception as e:
            raise InvalidArguments(str(e))

        async def _op(obj: GetAttrObjectT) -> FilterResultT:
            attribute_values = await self._get_attribute_values(obj, attribute_name)
            reference_value_expander = self.value_expanders.get(
                attribute_name, self._to_list
            )
            ref_values_expanded = reference_value_expander(reference_value)
            if inspect.iscoroutine(ref_values_expanded):
                ref_values_expanded = await typing.cast(
                    typing.Awaitable[typing.Any], ref_values_expanded
                )

            return iterable_op(
                binary_op(attribute_value, ref_value)
                for attribute_value in attribute_values
                for ref_value in ref_values_expanded
            )

        return _op

    def _handle_unary_op(
        self, unary_op: UnaryOperatorT[FilterResultT], nodes: TreeT
    ) -> CompiledTreeT[GetAttrObject, FilterResultT]:
        element = self.build_evaluator(nodes)

        async def _unary_op(values: GetAttrObjectT) -> FilterResultT:
            return unary_op(await element(values))

        return _unary_op

    def _handle_multiple_op(
        self,
        multiple_op: MultipleOperatorT[FilterResultT],
        nodes: typing.Iterable[
            typing.Union[TreeT, CompiledTreeT[GetAttrObject, FilterResultT]]
        ],
    ) -> CompiledTreeT[GetAttrObject, FilterResultT]:
        elements = [self.build_evaluator(node) for node in nodes]

        async def _multiple_op(values: GetAttrObjectT) -> FilterResultT:
            return multiple_op([await element(values) for element in elements])

        return _multiple_op


def BinaryFilter(
    tree: typing.Union[TreeT, CompiledTreeT[GetAttrObject, bool]],
) -> "Filter[bool]":
    return Filter[bool](
        tree,
        {"-": operator.not_},
        {
            "=": (operator.eq, any, _identity),
            "<": (operator.lt, any, _identity),
            ">": (operator.gt, any, _identity),
            "<=": (operator.le, any, _identity),
            ">=": (operator.ge, any, _identity),
            "!=": (operator.ne, all, _identity),
            "~=": (lambda a, b: a is not None and b.search(a), any, re.compile),
        },
        {
            "or": any,
            "and": all,
        },
    )


def _minimal_datetime(dts: typing.Iterable[object]) -> datetime.datetime:
    _dts = list(typing.cast(typing.List[datetime.datetime], Filter._to_list(dts)))
    if len(_dts) == 0:
        return date.DT_MAX
    else:
        return min(_dts)


def _as_datetime(value: typing.Any) -> datetime.datetime:
    if isinstance(value, datetime.datetime):
        return value
    elif isinstance(value, datetime.timedelta):
        dt = utils.utcnow()
        return dt + value
    elif isinstance(value, date.PartialDatetime):
        dt = utils.utcnow().replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if isinstance(value, date.DayOfWeek):
            return dt + datetime.timedelta(days=value.value - dt.isoweekday())
        elif isinstance(value, date.Day):
            return dt.replace(day=value.value)
        elif isinstance(value, date.Month):
            return dt.replace(month=value.value, day=1)
        elif isinstance(value, date.Year):
            return dt.replace(year=value.value, month=1, day=1)
        else:
            return date.DT_MAX
    elif isinstance(value, datetime.time):
        return utils.utcnow().replace(
            hour=value.hour,
            minute=value.minute,
            second=value.second,
            microsecond=value.microsecond,
        )
    else:
        return date.DT_MAX


def _dt_max(value: typing.Any, ref: typing.Any) -> datetime.datetime:
    return date.DT_MAX


def _dt_identity_max(value: typing.Any) -> datetime.datetime:
    return date.DT_MAX


def _dt_op(
    op: typing.Callable[[typing.Any, typing.Any], bool],
) -> typing.Callable[[typing.Any, typing.Any], datetime.datetime]:
    def _operator(value: typing.Any, ref: typing.Any) -> datetime.datetime:
        try:
            dt_value = _as_datetime(value)
            dt_ref = _as_datetime(ref)
            handle_equality = op in (
                operator.eq,
                operator.ne,
                operator.le,
                operator.ge,
            )
            if handle_equality and dt_value == dt_ref:
                # NOTE(sileht): The condition will change...
                if isinstance(ref, date.PartialDatetime):
                    if isinstance(value, date.DayOfWeek):
                        # next day
                        dt_ref = dt_ref + datetime.timedelta(days=1)
                    elif isinstance(ref, date.Day):
                        # next day
                        dt_ref = dt_ref + datetime.timedelta(days=1)
                    elif isinstance(ref, date.Month):
                        # first day of next month
                        dt_ref = dt_ref.replace(day=1)
                        dt_ref = dt_ref + datetime.timedelta(days=32)
                        dt_ref = dt_ref.replace(day=1)
                    elif isinstance(ref, date.Year):
                        # first day of next year
                        dt_ref = dt_ref.replace(month=1, day=1)
                        dt_ref = dt_ref + datetime.timedelta(days=366)
                        dt_ref = dt_ref.replace(month=1, day=1)
                    return dt_ref.replace(hour=0, minute=0, second=0, microsecond=0)
                else:
                    return dt_ref + datetime.timedelta(minutes=1)
            elif dt_value < dt_ref:
                return dt_ref
            else:
                if isinstance(ref, datetime.time):
                    # Condition will change next day at 00:00:00
                    dt_ref = dt_ref + datetime.timedelta(days=1)
                elif isinstance(value, date.DayOfWeek):
                    dt_ref = dt_ref + datetime.timedelta(days=7)
                elif isinstance(ref, date.Day):
                    # Condition will change, 1st day of next month at 00:00:00
                    dt_ref = dt_ref.replace(day=1)
                    dt_ref = dt_ref + datetime.timedelta(days=32)
                    if op in (operator.eq, operator.ne):
                        dt_ref = dt_ref.replace(day=ref.value)
                    else:
                        dt_ref = dt_ref.replace(day=1)
                elif isinstance(ref, date.Month):
                    # Condition will change, 1st January of next year at 00:00:00
                    dt_ref = dt_ref.replace(month=1, day=1)
                    dt_ref = dt_ref + datetime.timedelta(days=366)
                    if op in (operator.eq, operator.ne):
                        dt_ref = dt_ref.replace(month=ref.value, day=1)
                    else:
                        dt_ref = dt_ref.replace(month=1, day=1)
                else:
                    return date.DT_MAX
                if op in (operator.eq, operator.ne):
                    return dt_ref
                else:
                    return dt_ref.replace(hour=0, minute=0, second=0, microsecond=0)
        except OverflowError:
            return date.DT_MAX

    return _operator


def NearDatetimeFilter(
    tree: typing.Union[TreeT, CompiledTreeT[GetAttrObject, datetime.datetime]],
) -> "Filter[datetime.datetime]":
    """
    The principes:
    * the attribute can't be mapped to a datetime -> datetime.datetime.max
    * the time/datetime attribute can't change in the future -> datetime.datetime.max
    * the time/datetime attribute can change in the future -> return when
    * we have a list of time/datetime, we pick the more recent
    """
    return Filter[datetime.datetime](
        tree,
        # NOTE(sileht): This is not allowed in parser on all time based attributes
        # so we can just return DT_MAX for all other attributes
        {"-": _dt_identity_max},
        {
            "=": (_dt_op(operator.eq), _minimal_datetime, _identity),
            "<": (_dt_op(operator.lt), _minimal_datetime, _identity),
            ">": (_dt_op(operator.gt), _minimal_datetime, _identity),
            "<=": (_dt_op(operator.le), _minimal_datetime, _identity),
            ">=": (_dt_op(operator.ge), _minimal_datetime, _identity),
            "!=": (_dt_op(operator.ne), _minimal_datetime, _identity),
            # NOTE(sileht): This is not allowed in parser on all time based attributes
            # so we can just return DT_MAX for all other attributes
            "~=": (_dt_max, _minimal_datetime, re.compile),
        },
        {
            "or": _minimal_datetime,
            "and": _minimal_datetime,
        },
    )
