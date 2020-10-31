#!/bin/bash

set -e
set -o pipefail
set -x

repo_dir=$(readlink -f $(dirname $(readlink -f $0))/..)

# We put it in .build to make netlify cache the python installation
export PYENV_ROOT="$repo_dir/docs/.build/pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"

if [ ! -d "$PYENV_ROOT" ]; then
    git clone --depth 1 https://github.com/pyenv/pyenv.git $PYENV_ROOT
else
    (cd $PYENV_ROOT && git pull)
fi

eval "$(pyenv init -)"
version=$(awk -F '-' '{print $2}' $repo_dir/runtime.txt)
pyenv install -s $version
pyenv shell $version
pip install tox

cd $repo_dir

tox -e docs
