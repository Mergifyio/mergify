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
import inspect
import operator
import re
import typing

from mergify_engine.rules import parser


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


def _identity(value):
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
        "or": typing.Iterable[typing.Union["TreeT", "CompiledTreeT[GetAttrObject]"]],  # type: ignore[misc]
        "and": typing.Iterable[typing.Union["TreeT", "CompiledTreeT[GetAttrObject]"]],  # type: ignore[misc]
    },
    total=False,
)


class GetAttrObject(typing.Protocol):
    def __getattribute__(self, key: typing.Any) -> typing.Any:
        ...


GetAttrObjectT = typing.TypeVar("GetAttrObjectT", bound=GetAttrObject)

CompiledTreeT = typing.Callable[[GetAttrObjectT], typing.Awaitable[bool]]

UnaryOperatorT = typing.Callable[[typing.Any], bool]
BinaryOperatorT = typing.Tuple[
    typing.Callable[[typing.Any, typing.Any], bool],
    typing.Callable[[typing.Iterable[object]], bool],
    typing.Callable[[typing.Any], typing.Any],
]
MultipleOperatorT = typing.Callable[..., bool]


@dataclasses.dataclass(repr=False)
class Filter:
    tree: TreeT

    unary_operators: typing.ClassVar[typing.Dict[str, UnaryOperatorT]] = {
        "-": operator.not_
    }

    binary_operators: typing.ClassVar[typing.Dict[str, BinaryOperatorT]] = {
        "=": (operator.eq, any, _identity),
        "<": (operator.lt, any, _identity),
        ">": (operator.gt, any, _identity),
        "<=": (operator.le, any, _identity),
        ">=": (operator.ge, any, _identity),
        "!=": (operator.ne, all, _identity),
        "~=": (lambda a, b: a is not None and b.search(a), any, re.compile),
    }

    multiple_operators: typing.ClassVar[typing.Dict[str, MultipleOperatorT]] = {
        "or": any,
        "and": all,
    }

    value_expanders: typing.Dict[
        str, typing.Callable[[typing.Any], typing.List[typing.Any]]
    ] = dataclasses.field(default_factory=dict)

    _eval: typing.Callable[
        ["Filter", GetAttrObjectT], typing.Awaitable[bool]
    ] = dataclasses.field(init=False)

    def __post_init__(self) -> None:
        # https://github.com/python/mypy/issues/2427
        self._eval = self.build_evaluator(self.tree)  # type: ignore[assignment]

    @classmethod
    def parse(cls, string: str) -> "Filter":
        return cls(parser.search.parseString(string, parseAll=True)[0])

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
            return str(nodes[0]) + op + str(nodes[1])
        raise InvalidOperator(op)  # pragma: no cover

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}({str(self)})"

    async def __call__(self, obj: GetAttrObjectT) -> bool:
        return await self._eval(obj)

    LENGTH_OPERATOR = "#"

    def _to_list(self, item: typing.Any) -> typing.List[typing.Any]:
        if isinstance(item, list):
            return item

        if isinstance(item, tuple):
            return list(item)

        return [item]

    async def _get_attribute_values(
        self,
        obj: GetAttrObjectT,
        attribute_name: str,
    ) -> typing.List[typing.Any]:
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
        self, tree: typing.Union[TreeT, CompiledTreeT[GetAttrObject]]
    ) -> CompiledTreeT[GetAttrObject]:
        if callable(tree):
            return tree

        if len(tree) != 1:
            raise ParseError(tree)

        operator_name, nodes = list(tree.items())[0]
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
        nodes = typing.cast(typing.Iterable[TreeT], nodes)
        return self._handle_multiple_op(multiple_op, nodes)

    def _handle_binary_op(
        self,
        op: BinaryOperatorT,
        nodes: TreeBinaryLeafT,
    ) -> CompiledTreeT[GetAttrObject]:
        if len(nodes) != 2:
            raise InvalidArguments(nodes)

        binary_op, iterable_op, compile_fn = op
        try:
            attribute_name, reference_value = (nodes[0], compile_fn(nodes[1]))
        except Exception as e:
            raise InvalidArguments(str(e))

        async def _op(obj: GetAttrObjectT) -> bool:
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
        self, unary_op: UnaryOperatorT, nodes: TreeT
    ) -> CompiledTreeT[GetAttrObject]:
        element = self.build_evaluator(nodes)

        async def _unary_op(values: GetAttrObjectT) -> bool:
            return unary_op(await element(values))

        return _unary_op

    def _handle_multiple_op(
        self,
        multiple_op: MultipleOperatorT,
        nodes: typing.Iterable[typing.Union[TreeT, CompiledTreeT[GetAttrObject]]],
    ) -> CompiledTreeT[GetAttrObject]:
        elements = [self.build_evaluator(node) for node in nodes]

        async def _multiple_op(values: GetAttrObjectT) -> bool:
            return multiple_op([await element(values) for element in elements])

        return _multiple_op
