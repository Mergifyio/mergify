import glob

import yaml


for fname in glob.glob("datadog/conf.d/*.yaml"):
    with open(fname) as f:
        yaml.safe_load(f)
