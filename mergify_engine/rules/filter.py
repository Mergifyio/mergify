# -*- encoding: utf-8 -*-
#
# Copyright © 2018 Julien Danjou <jd@mergify.io>
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
import operator
import re

import attr

from mergify_engine.rules import parser


class InvalidQuery(Exception):
    pass


class ParseError(InvalidQuery):
    def __init__(self, tree):
        super().__init__("Unable to parse tree: %s" % str(tree))
        self.tree = tree


class UnknownAttribute(InvalidQuery, ValueError):
    def __init__(self, key):
        super().__init__("Unknown attribute: %s" % str(key))
        self.key = key


class UnknownOperator(InvalidQuery, ValueError):
    def __init__(self, operator):
        super().__init__("Unknown operator: %s" % str(operator))
        self.operator = operator


class InvalidOperator(InvalidQuery, TypeError):
    def __init__(self, operator):
        super().__init__("Invalid operator: %s" % str(operator))
        self.operator = operator


class InvalidArguments(InvalidQuery, ValueError):
    def __init__(self, arguments):
        super().__init__("Invalid arguments: %s" % str(arguments))
        self.arguments = arguments


@attr.s(str=False, repr=False)
class Filter:
    unary_operators = {
        "-": operator.not_,
        "¬": operator.not_,
    }

    binary_operators = {
        "=": (operator.eq, any),
        "==": (operator.eq, any),

        "<": (operator.lt, any),

        ">": (operator.gt, any),

        "<=": (operator.le, any),
        "≤": (operator.le, any),

        ">=": (operator.ge, any),
        "≥": (operator.ge, any),

        "!=": (operator.ne, all),
        "≠": (operator.ne, all),

        "~=": (lambda a, b: re.search(b, a), any),
    }

    tree = attr.ib()
    # The name of the attribute that is going to be evaluated by this filter.
    attribute_name = attr.ib(init=False)

    def __attrs_post_init__(self):
        self._eval = self.build_evaluator(self.tree)

    @classmethod
    def parse(cls, string):
        return cls(parser.search.parseString(string, parseAll=True)[0])

    def __str__(self):
        return self._tree_to_str(self.tree)

    def _tree_to_str(self, tree):
        # We don't do any kind of validation here since build_evaluator does
        # that.
        operator, nodes = list(tree.items())[0]
        if operator in self.unary_operators:
            return operator + self._tree_to_str(nodes)
        if operator in self.binary_operators:
            return str(nodes[0]) + operator + str(nodes[1])
        raise RuntimeError(
            "Unable to convert tree to string: unknown operator: %s"
            % operator)  # pragma: no cover

    def __repr__(self):  # pragma: no cover
        return "%s(%s)" % (self.__class__.__name__, str(self))

    def __call__(self, **kwargs):
        return self._eval(kwargs)

    LENGTH_OPERATOR = "#"
    ATTR_SEPARATOR = "."

    @staticmethod
    def _identity(value):
        return value

    def _resolve_name(self, values, name):
        if name.startswith(self.LENGTH_OPERATOR):
            self.attribute_name = name[1:]
            op = len
        else:
            self.attribute_name = name
            op = self._identity
        try:
            for subname in self.attribute_name.split(self.ATTR_SEPARATOR):
                values = values[subname]
            try:
                return op(values)
            except TypeError:
                raise InvalidOperator(name)
        except KeyError:
            raise UnknownAttribute(self.attribute_name)

    def build_evaluator(self, tree):
        items = list(tree.items())
        if len(items) != 1:
            raise ParseError(tree)
        try:
            operator, nodes = items[0]
        except Exception:  # pragma: no cover
            raise ParseError(tree)
        try:
            op = self.unary_operators[operator]
        except KeyError:
            try:
                op, iterable_op = self.binary_operators[operator]
            except KeyError:
                raise UnknownOperator(operator)
            if len(nodes) != 2:
                raise InvalidArguments(nodes)

            def _op(values):
                values = self._resolve_name(values, nodes[0])
                if isinstance(values, (list, tuple)) and op != len:
                    return iterable_op(map(lambda x: op(x, nodes[1]), values))
                return op(values, nodes[1])
            return _op
        element = self.build_evaluator(nodes)
        return lambda values: op(element(values))
