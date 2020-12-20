#!/bin/bash

TESTS="$(pytest --collect-only | sed -n -e '/TestCaseFunction/s/.* \([^ >]*\)>.*/\1/gp')"
ret=0
for f in $(ls -d1 zfixtures/cassettes/*/* | awk -F/ '{print $4}' ); do
    used=$(echo -e "$TESTS" | grep "^$f\$")
    if [ ! "$used" ]; then
        if [ "$ret" -eq 0 ] ; then
            echo "- This directory are unused by tests:"
        fi
        echo zfixtures/cassettes/*/$f unused
        ret=1
    fi
done
exit $ret
