# -*- coding: utf-8 -*-
import datetime
import os
import sys

import pkg_resources


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

extensions = ["sphinx.ext.githubpages", "sphinx.ext.graphviz", "mergify_engine_gendoc"]

graphviz_dot_args = ["-Gdpi=200"]

# Add any paths that contain templates here, relative to this directory.
templates_path = ["_templates"]
master_doc = "index"

# General information about the project.
project = "Mergify"
copyright = "%s, Mergify" % datetime.date.today().year
version = pkg_resources.get_distribution("mergify_engine").version

# The name of the Pygments (syntax highlighting) style to use.
pygments_style = "sphinx"

html_logo = "_static/mergify-logo-horizontal.png"
html_favicon = "_static/favicon.ico"
html_static_path = ["_static"]
# html_sidebars = {
#    "**": ["about.html", "navigation.html", "relations.html", "searchbox.html"]
# }
html_show_sourcelink = False
# If true, "Created using Sphinx" is shown in the HTML footer. Default is True.
html_show_sphinx = False
# Our templates are based on this
html_theme = "basic"
html_sidebars = {
    "**": [
        "navigation.html",
        "relations.html",
    ]
}
html_add_permalinks = " 🔗"
