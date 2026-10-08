import json
import sys
sys.path.insert(0, "<desc-repo>")
import os
from pathlib import Path

import mutmut
import mutmut.__main__ as mm

from scripts import run_mutation_gate as g

SRC = "src/osm_polygon_description_tag/dataset/docs.py"
mutmut._reset_globals()
os.environ["MUTANT_UNDER_TEST"] = "mutation_generation"
g._configure_mutmut(mutmut, only_mutate=[SRC], test_selection=(), changed_lines=None)
g.MUTANTS_DIR.mkdir(exist_ok=True)
mm.copy_src_dir()
mm.copy_also_copy_files()
mm.setup_source_paths()
mm.create_mutants(8)
meta = json.loads(Path("mutants", SRC + ".meta").read_text(encoding="utf-8"))
keys = list(meta["exit_code_by_key"].keys())
print("docs_mutants_count", len(keys))
print("pre_set_results", sum(1 for k in keys if meta["exit_code_by_key"][k] is not None))
for k in keys[:3]:
    print("example", k)
