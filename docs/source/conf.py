# -*- coding: utf-8 -*-
import datetime
import os
import sys

import pkg_resources


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

extensions = [
    "sphinxcontrib.spelling",
    "sphinx.ext.githubpages",
    "sphinx.ext.graphviz",
    "mergify_engine_gendoc",
]

graphviz_dot_args = [
    "-Gdpi=200",
    "-Gnodesep=0.15",
    "-Granksep=0.25",
    "-Gfontname=Open Sans",
    "-Efontname=Open Sans",
    "-Nfontname=Open Sans",
    "-Gfontsize=8pt",
    "-Efontsize=8pt",
    "-Nfontsize=8pt",
    "-Npenwidth=1.75",
    "-Epenwidth=1.75",
    "-Gpenwidth=1.75",
]

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
html_style = "mergify.css"
html_sidebars = {
    "**": [
        "navigation.html",
        "relations.html",
    ]
}
html_permalinks_icon = "🔗"

# Spelling checker configuration
spelling_warning = True
spelling_show_whole_line = False
