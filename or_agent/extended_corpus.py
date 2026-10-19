"""Reproducible, independently checked GAP corpus; never contacts an LLM."""

import argparse
from collections import Counter
import hashlib
import itertools
import json
from pathlib import Path
import random
import re

from ortools.sat.python import cp_model

from .evaluate import enumerate_reference
from .models import Gap, load_json
from .solver import build_model
from .verify import verify


DEFAULT_PATH = Path("data/extended-benchmark.json")
VERSION = "extended-m5-v1"

# Domains identify independently authored operational families, not renamed copies
# of a shared numerical instance. Each line supplies its own task/resource sets.
DEV_DOMAINS = """astronomy|Reduce,Calibrate,Stack|DomeA,DomeB|minute
seedbank|Dry,Weigh,Archive,Index|RoomA,RoomB|tray
cartography|Trace,Label,Render|StationA,StationB,StationC|minute
aquarium|Test,Clean,Feed,Inspect|KeeperA,KeeperB|minute
bookbinding|Stitch,Press,Trim|BenchA,BenchB|sheet
meteorology|Decode,Validate,Plot,Publish|NodeA,NodeB,NodeC|slot
archaeology|Wash,Record,Photograph|LabA,LabB|minute
theatre|Focus,Test,Patch,Program|DeskA,DeskB|minute
ceramics|Throw,Dry,Glaze,Fire,Polish,Stamp,Pack|KilnA,KilnB,KilnC|unit
compost|Sort,Shred,Mix,Turn,Sieve,Test,Bag,Label|BayA,BayB,BayC|unit
microfilm|Clean,Scan,Align,Read,Index,Store,Check|ScannerA,ScannerB,ScannerC|minute
bicycle|Brake,Wheel,Chain,Frame,Saddle,Cable,Light|StandA,StandB,StandC,StandD|minute
sonar|Filter,Beam,Detect,Track,Merge,Log,Export,Review|BoardA,BoardB,BoardC|slot
botany|Sow,Water,Thin,Measure,Sample,Press,Label|TeamA,TeamB,TeamC|minute
photometry|Expose,Read,Subtract,Normalize,Fit,Plot,Archive|NodeD,NodeE,NodeF|slot
loom|Warp,Thread,Weave,Inspect,Cut,Fold,Bundle|LoomA,LoomB,LoomC|unit
embroidery|Hoop,Thread,Stitch,Trim|FrameA,FrameB,FrameC|minute
calligraphy|Rule,Ink,Dry,Seal|DeskD,DeskE|sheet
herbarium|Press,Mount,Describe,Digitize|TableA,TableB,TableC|minute
batik|Wax,Dye,Rinse,Finish|VatA,VatB|unit
glaciology|Drill,Core,Measure,Store|CrewA,CrewB,CrewC|minute
origami|Crease,Fold,Lock,Decorate|StationD,StationE|sheet
manuscripts|Collate,Read,Compare,Catalogue|ReaderA,ReaderB,ReaderC|minute"""

SIMPLE_DOMAINS = """bakery|Knead,Proof,Bake|OvenA,OvenB|batch
laundry|Wash,Rinse,Spin,Dry|DrumA,DrumB|kilogram
dentistry|Image,Scale,Polish|RoomA,RoomB,RoomC|minute
parcelhub|Weigh,Scan,Seal,Sort|BeltA,BeltB|slot
tailoring|Measure,Cut,Sew|TableA,TableB|minute
coffee|Roast,Grind,Brew,Pack|CellA,CellB,CellC|unit
waterlab|Acidity,Salinity,Turbidity|BenchA,BenchB|sample
printshop|Poster,Leaflet,Card,Ticket|PressA,PressB|sheet
foundry|Cast,Cool,Trim|MouldA,MouldB,MouldC|unit
translation|Legal,Travel,Science,Poetry|EditorA,EditorB|minute
recycling|Glass,Paper,Metal|LineA,LineB|kilogram
bloodbank|Type,Screen,Label,Chill|UnitA,UnitB,UnitC|sample
furniture|Plane,Drill,Join|BenchC,BenchD|minute
cheesemaking|Curd,Salt,Press,Wrap|VatA,VatB|unit
watchmaking|Gears,Hands,Case|DeskA,DeskB,DeskC|minute
library|Return,Shelve,Repair,Register|TeamA,TeamB|book
aquaculture|Grade,Sample,Tag|TankA,TankB|unit
optics|Cut,Grind,Coat,Test|RigA,RigB,RigC|minute
woodfuel|Split,Chip,Dry|MillA,MillB|kilogram
textilelab|Tensile,Abrasion,Colour,Wash|RigD,RigE|sample
beekeeping|Inspect,Extract,Filter|CrewA,CrewB,CrewC|minute
prosthetics|Scan,Shape,Finish,Fit|LabA,LabB|minute
instrumentrepair|Tune,Pad,Align|ShopA,ShopB|minute
soapmaking|Mix,Cure,Cut,Wrap|RackA,RackB,RackC|unit
filmarchive|Inspect,Clean,Splice|DeskD,DeskE|reel"""

COMBINATORIAL_DOMAINS = """pharmacy|Count,Blend,Fill,Cap,Label,Inspect,Box|CellA,CellB,CellC|minute
semiconductors|Etch,Dope,Coat,Clean,Probe,Dice,Bond,Seal|ToolA,ToolB,ToolC|slot
satellite|Decode,Correct,Compress,Encrypt,Route,Store,Audit|CoreA,CoreB,CoreC,CoreD|slot
shipyard|Plate,Weld,Grind,Paint,Wire,Plumb,Inspect,Launch|CrewA,CrewB,CrewC|minute
hospitalsterilization|Collect,Wash,Rinse,Dry,Inspect,Wrap,Sterilize|BayA,BayB,BayC|tray
geochemistry|Crush,Mill,Dissolve,Filter,Dilute,Assay,Report,Retain|LabA,LabB,LabC|sample
animation|Layout,Model,Rig,Texture,Light,Render,Composite|NodeA,NodeB,NodeC,NodeD|slot
brewery|Mill,Mash,Lauter,Boil,Cool,Ferment,Filter,Bottle|TankA,TankB,TankC|unit
aircraftservice|Tyres,Brakes,Hydraulics,Electrics,Cabin,Avionics,Structure|TeamA,TeamB,TeamC|minute
cosmetics|Weigh,Emulsify,Cool,Sample,Approve,Fill,Label,Pack|LineA,LineB,LineC|unit
metrology|Length,Mass,Force,Pressure,Voltage,Current,Temperature|RigA,RigB,RigC,RigD|minute
quarry|Drill,Blast,Load,Crush,Sieve,Wash,Stock,Dispatch|CellD,CellE,CellF|unit
oceanography|Salinity,Oxygen,Nitrate,Phosphate,Carbon,Alkalinity,Silicate|LabD,LabE,LabF|sample
leatherworks|Soak,Lime,Delime,Tan,Dye,Stretch,Finish,Grade|DrumA,DrumB,DrumC|unit
robotics|Vision,Grip,Move,Insert,Fasten,Measure,Release|ArmA,ArmB,ArmC,ArmD|slot
flourmill|Clean,Condition,Break,Sift,Purify,Reduce,Blend,Bag|MillA,MillB,MillC|unit
genomics|Extract,Quantify,Fragment,Adapt,Amplify,Sequence,Analyse|LabG,LabH,LabI|sample
locomotive|Engine,Bogie,Axle,Brake,Coupler,Cab,Paint,Test|ShopA,ShopB,ShopC|minute
packaging|Print,Laminate,Slit,Score,Fold,Glue,Bundle|LineD,LineE,LineF,LineG|unit
forensics|Receive,Image,Sample,Extract,Profile,Compare,Review,Seal|BenchA,BenchB,BenchC|sample
winery|Destem,Crush,Press,Clarify,Ferment,Rack,Bottle|VatA,VatB,VatC|unit
solarassembly|Glass,Cells,String,Layup,Laminate,Frame,Connect,Test|CellG,CellH,CellI|minute
perfumery|Measure,Macerate,Blend,Filter,Age,Fill,Crimp|DeskA,DeskB,DeskC,DeskD|unit
conservation|Survey,Consolidate,Clean,Fill,Retouch,Protect,Document,Crate|TeamD,TeamE,TeamF|minute
diagnostics|Receive,Aliquot,Centrifuge,Stain,Count,Read,Validate|AnalyzerA,AnalyzerB,AnalyzerC|sample"""

LANGUAGE_DOMAINS = """chocolate|Temper,Mould,Cool,Wrap|VatD,VatE,VatF|unit
stainedglass|Trace,Cut,Lead,Solder|BenchE,BenchF|minute
audio|Edit,Equalize,Compress,Master|RoomD,RoomE,RoomF|minute
ceremonial|Fold,Arrange,Check,Deliver|TeamG,TeamH|unit
veterinary|Examine,Image,Dress,Observe|RoomG,RoomH,RoomI|minute
entomology|Sort,Pin,Image,Identify|DeskE,DeskF|specimen
velodrome|Sweep,Mark,Inspect,Measure|CrewD,CrewE,CrewF|minute
foodtesting|Moisture,Ash,Fat,Protein|BenchG,BenchH|sample
upholstery|Strip,Pad,Cover,Finish|StandC,StandD,StandE|minute
flagmaking|Dye,Cut,Join,Hem|TableC,TableD|sheet
harpsichord|String,Voice,Tune,Polish|ShopD,ShopE,ShopF|minute
magazine|Copy,Proof,Layout,Export|EditorC,EditorD|page
orchard|Thin,Prune,Inspect,Harvest|CrewG,CrewH,CrewI|minute
droneimaging|Correct,Stitch,Classify,Export|NodeE,NodeF|slot
spicepacking|Sieve,Blend,Fill,Seal|LineH,LineI,LineJ|unit
coins|Clean,Weigh,Image,Grade|DeskG,DeskH|specimen
papermaking|Pulp,Form,Press,Dry|VatG,VatH,VatI|sheet
costume|Cut,Fit,Stitch,Decorate|TableE,TableF|minute
embalming|Prepare,Treat,Dress,Document|RoomJ,RoomK,RoomL|minute
lapidary|Saw,Grind,Polish,Mount|WheelA,WheelB|minute
teablending|Taste,Weigh,Blend,Pack|DeskI,DeskJ,DeskK|unit
signwriting|Layout,Paint,Dry,Coat|BenchI,BenchJ|minute
sculpture|Rough,Carve,Smooth,Wax|StandF,StandG,StandH|minute
quiltmaking|Cut,Piece,Layer,Bind|TableG,TableH|sheet
radioarchive|Capture,Clean,Tag,Publish|NodeG,NodeH,NodeI|minute"""


def sha(value):
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def semantic_signature(problem):
    """Ignore names, task ordering, resource ordering, and unit spelling."""
    p = Gap.model_validate(problem)
    forms = []
    for order in itertools.permutations(range(len(p.resources))):
        columns = sorted(tuple((p.costs[i][j], p.consumption[i][j]) for i in order)
                         for j in range(len(p.tasks)))
        forms.append((tuple(p.capacities[i] for i in order), tuple(columns)))
    return sha(min(forms))


def domains(text):
    return [line.split("|") for line in text.splitlines()]


def make_problem(row, seed, challenge):
    _, tasks, resources, unit = row
    tasks, resources = tasks.split(","), resources.split(",")
    n, m = len(tasks), len(resources)
    rng = random.Random(seed)
    costs = [[rng.randrange(1, 40) for _ in tasks] for _ in resources]
    consumption = [[rng.randrange(1, 7) for _ in tasks] for _ in resources]
    owners = [j % m for j in range(n)]
    capacities = [sum(consumption[i][j] for j in range(n) if owners[j] == i) for i in range(m)]
    if challenge == "loose_capacity":
        capacities = [sum(row) + 1 for row in consumption]
    elif challenge == "uniform_usage":
        consumption = [[1 + seed % 3] * n for _ in resources]
        capacities = [(1 + seed % 3) * ((n + m - 1) // m) for _ in resources]
    elif challenge == "rebates":
        costs[0][0] = -rng.randrange(1, 15)
        costs[-1][-1] = 0
    elif challenge == "free_usage":
        consumption[0][0] = 0
        capacities[0] = sum(consumption[0][j] for j in range(n) if owners[j] == 0)
    elif challenge == "greedy_trap":
        # The first two jobs compete for resource 0; the expensive fallback matters.
        consumption = [[1] * n for _ in resources]
        capacities = [1] + [(n + m - 2) // (m - 1)] * (m - 1)
        for i in range(m):
            costs[i][0], costs[i][1] = (0, 1) if i == 0 else (2 + i, 80 + i)
    elif challenge == "multiple_optima":
        # Two distinct feasible assignments have equal cost; capacities still bind.
        values = [rng.randrange(1, 25) for _ in tasks]
        costs = [values.copy() for _ in resources]
        alternate = owners.copy()
        alternate[0], alternate[1] = alternate[1], alternate[0]
        capacities = [max(capacities[i], sum(consumption[i][j] for j in range(n) if alternate[j] == i))
                      for i in range(m)]
    elif challenge == "capacity_exclusions":
        for j in range(n):
            excluded = (owners[j] + 1) % m
            consumption[excluded][j] = capacities[excluded] + 1
    elif challenge == "packing_infeasible":
        consumption = [[2] * n for _ in resources]
        mode = seed % 5
        if mode == 0:
            capacities = [2 * max(0, (n - 1) // m)] * m
        elif mode == 1:
            capacities = [3] * m
            if m >= n:
                capacities = [2] + [1] * (m - 1)
        elif mode == 2:
            capacities = [6 + i for i in range(m)]
            for i in range(m):
                consumption[i][0] = capacities[i] + 1
        elif mode == 3:
            capacities = [4] + [2 * n] * (m - 1)
            for i in range(1, m):
                for j in range(3):
                    consumption[i][j] = capacities[i] + 1
        else:
            capacities = [4, 4] + [2 * n] * (m - 2)
            for i in range(2, m):
                for j in range(n - 1):
                    consumption[i][j] = capacities[i] + 1
    return dict(tasks=tasks, resources=resources, costs=costs, consumption=consumption,
                capacities=capacities, cost_unit="euro", resource_unit=unit)


def make_development_problem(row, seed, index, infeasible=False):
    """Development uses different demand recipes from the held-out generators."""
    _, tasks, resources, unit = row
    tasks, resources = tasks.split(","), resources.split(",")
    n, m = len(tasks), len(resources)
    rng = random.Random(seed)
    recipe = ("linear_demands", "resource_multipliers", "dedicated_eligibility", "cyclic_demands")[index % 4]
    costs = [[(i + 1) * (j + 1) ** 2 + rng.randrange(9) - (10 if index % 5 == 0 and j == 0 else 0)
              for j in range(n)] for i in range(m)]
    if recipe == "linear_demands":
        consumption = [[j + 1 for j in range(n)] for _ in resources]
    elif recipe == "resource_multipliers":
        consumption = [[(i + 1) * (j + 1) for j in range(n)] for i in range(m)]
    elif recipe == "dedicated_eligibility":
        consumption = [[rng.randrange(1, 6) for _ in tasks] for _ in resources]
    else:
        consumption = [[1 + (j + i) % n for j in range(n)] for i in range(m)]
    owners = [(j + index) % m for j in range(n)]
    capacities = [sum(consumption[i][j] for j in range(n) if owners[j] == i) + index % 2 for i in range(m)]
    if recipe == "dedicated_eligibility":
        for i in range(m):
            for j in range(n):
                if owners[j] != i:
                    consumption[i][j] = capacities[i] + 1
    if infeasible:
        capacities = [0] * m
        recipe += "_zero_capacity"
    return dict(tasks=tasks, resources=resources, costs=costs, consumption=consumption,
                capacities=capacities, cost_unit="euro", resource_unit=unit), recipe


def render_source(problem, context, style, difficulty=False, development=False):
    p = problem
    tasks, resources = p["tasks"], p["resources"]
    intro = (f"At the {context} workshop, every listed task is mandatory and indivisible. "
             "Assign each task to exactly one listed resource. Minimize the sum of assignment costs. "
             "Each resource limit applies cumulatively to its assigned tasks. There are no other constraints.")
    if difficulty:
        prefaces = [
            "La réunion évoque aussi un calendrier; aucun horaire, délai ni ordre de passage n'est demandé. ",
            "Do not confuse an unused resource with an optional task: every task remains compulsory. ",
            "Ces montants sont des coûts, pas des bénéfices; les remboursements négatifs restent négatifs. ",
            "Only the signed values in the current specification below are operative; anecdotes add no constraints. ",
            "Lire les étiquettes: l'ordre des ressources peut changer entre les lignes. Ne pas transposer sans vérifier. ",
        ]
        intro = prefaces[style % len(prefaces)] + intro
    lines = [intro, "Tasks: " + ",".join(tasks) + ".", "Resources: " + ",".join(resources) + ".",
             f"Cost unit: {p['cost_unit']}. Resource unit: {p['resource_unit']}."]
    evidence = dict(tasks=[lines[1]], resources=[lines[2]], cost_unit=[lines[3]], resource_unit=[lines[3]],
                    costs=[], consumption=[], capacities=[])
    resource_order = list(range(len(resources)))
    if style % 2:
        resource_order.reverse()
    for i in resource_order:
        line = f"Cumulative capacity of {resources[i]}: {p['capacities'][i]} {p['resource_unit']}."
        lines.append(line)
        evidence["capacities"].append(line)
    if development:
        for j, task in enumerate(tasks):
            for i in resource_order:
                line = (f"Assignment record: T={task}; R={resources[i]}; cost={p['costs'][i][j]} {p['cost_unit']}; "
                        f"consumption={p['consumption'][i][j]} {p['resource_unit']}.")
                lines.append(line)
                evidence["costs"].append(line)
                evidence["consumption"].append(line)
    elif style % 3 == 0:
        for i in resource_order:
            line = f"Costs for {resources[i]}, task order {','.join(tasks)}: {','.join(map(str, p['costs'][i]))} {p['cost_unit']}."
            lines.append(line)
            evidence["costs"].append(line)
            line = f"Consumption for {resources[i]}, task order {','.join(tasks)}: {','.join(map(str, p['consumption'][i]))} {p['resource_unit']}."
            lines.append(line)
            evidence["consumption"].append(line)
    elif style % 3 == 1:
        for j in reversed(range(len(tasks))):
            order = resource_order if j % 2 else list(reversed(resource_order))
            for i in order:
                line = (f"Task {tasks[j]} on {resources[i]} costs {p['costs'][i][j]} {p['cost_unit']} "
                        f"and consumes {p['consumption'][i][j]} {p['resource_unit']}.")
                lines.append(line)
                evidence["costs"].append(line)
                evidence["consumption"].append(line)
    else:
        for j, task in enumerate(tasks):
            order = resource_order if j % 2 else list(reversed(resource_order))
            names = ",".join(resources[i] for i in order)
            line = (f"For task {task}, in resource order {names}, costs are "
                    f"{','.join(str(p['costs'][i][j]) for i in order)} {p['cost_unit']}; consumption is "
                    f"{','.join(str(p['consumption'][i][j]) for i in order)} {p['resource_unit']}.")
            lines.append(line)
            evidence["costs"].append(line)
            evidence["consumption"].append(line)
    return "\n".join(lines), evidence


def parse_source(source):
    """Read the literal statements independently of the rendering loops."""
    tasks = re.search(r"^Tasks: ([A-Za-z0-9_,\-]+)\.$", source, re.M).group(1).split(",")
    resources = re.search(r"^Resources: ([A-Za-z0-9_,\-]+)\.$", source, re.M).group(1).split(",")
    units = re.search(r"^Cost unit: ([^.]+)\. Resource unit: ([^.]+)\.$", source, re.M)
    capacities = dict(re.findall(r"^Cumulative capacity of (\w+): (\d+) [^.]+\.$", source, re.M))
    values = {"costs": {}, "consumption": {}}
    for kind, label in (("costs", "Costs"), ("consumption", "Consumption")):
        for resource, order, numbers in re.findall(
                rf"^{label} for (\w+), task order ([\w,]+): ([-\d,]+) [^.]+\.$", source, re.M):
            for task, number in zip(order.split(","), numbers.split(","), strict=True):
                values[kind][resource, task] = int(number)
    for task, resource, cost, usage in re.findall(
            r"^Task (\w+) on (\w+) costs (-?\d+) [^.]+ and consumes (\d+) [^.]+\.$", source, re.M):
        values["costs"][resource, task] = int(cost)
        values["consumption"][resource, task] = int(usage)
    for task, resource, cost, usage in re.findall(
            r"^Assignment record: T=(\w+); R=(\w+); cost=(-?\d+) [^;]+; consumption=(\d+) [^.]+\.$",
            source, re.M):
        values["costs"][resource, task] = int(cost)
        values["consumption"][resource, task] = int(usage)
    for task, order, costs, usages in re.findall(
            r"^For task (\w+), in resource order ([\w,]+), costs are ([-\d,]+) [^;]+; consumption is ([\d,]+) [^.]+\.$",
            source, re.M):
        for resource, cost, usage in zip(order.split(","), costs.split(","), usages.split(","), strict=True):
            values["costs"][resource, task] = int(cost)
            values["consumption"][resource, task] = int(usage)
    return dict(tasks=tasks, resources=resources, costs=[[values["costs"][r, t] for t in tasks] for r in resources],
                consumption=[[values["consumption"][r, t] for t in tasks] for r in resources],
                capacities=[int(capacities[r]) for r in resources], cost_unit=units.group(1), resource_unit=units.group(2))


def seal_case(case):
    case["source_sha256"] = sha(case["source"])
    case["problem_sha256"] = sha(case["problem"])
    case["semantic_sha256"] = (semantic_signature(case["problem"]) if case["problem"] is not None
                                else sha(case["reference_annotation"]))
    case["case_sha256"] = sha({k: v for k, v in case.items() if k != "case_sha256"})
    return case


def ready_case(row, split, category, index, seed, challenge):
    if split == "dev":
        problem, recipe = make_development_problem(row, seed, index, challenge == "packing_infeasible")
        source_style = "development_records"
    else:
        problem, recipe = make_problem(row, seed, challenge), challenge
        source_style = ("resource_rows", "pair_sentences", "task_rows")[index % 3]
    source, evidence = render_source(problem, row[0], index, category == "language", development=split == "dev")
    reference = enumerate_reference(problem)
    return seal_case(dict(id=f"{split}_{category}_{index:02d}", split=split, family=row[0], category=category,
                          challenge=challenge if category == "special" else recipe, source=source, disposition="ready", problem=problem,
                          generation_recipe=recipe, source_style=source_style,
                          reference_optimum=reference["optimum"], reference_feasible_count=reference["feasible_count"],
                          reference_optimum_count=reference["optimum_count"], field_evidence=evidence,
                          unit_equivalences={}, reference_annotation=dict(method="independent_exhaustive_enumeration",
                                                                        task_count=len(problem["tasks"]),
                                                                        assignment_count=len(problem["resources"]) ** len(problem["tasks"]))))


SPECIALS = [
    ("ambiguous", "clarify", "coldchain", "The limit of 6 box for ChillerA means either the sum of its assigned parcels or a separate limit per parcel; the author has not decided which.", "capacity_scope"),
    ("ambiguous", "clarify", "distillation", "Usage readings 3 and 5 are either litre or kilogram. Capacity is 9 kilogram; no density or choice of usage unit is supplied.", "usage_dimension"),
    ("ambiguous", "clarify", "tutoring", "Costs are written as 1,250 credit. The locale is unknown: this can mean one thousand two hundred fifty or one and a quarter.", "number_locale"),
    ("ambiguous", "clarify", "greenhouse", "Assign Water and Shade to North and South. 'Prefer lower cost unless usage is better' gives no objective priority or trade-off.", "objective_priority"),
    ("ambiguous", "clarify", "customs", "Resource labels Port and Dock may denote one shared resource or two separate resources. The author explicitly cannot resolve that identity.", "resource_identity"),
    ("incomplete", "clarify", "lithography", "Tasks Plate and Expose use presses LithoA and LithoB. All usage and capacity data are available, but the cost of Expose on LithoB has not been supplied.", "missing_pair_cost"),
    ("incomplete", "clarify", "radiology", "Assign Image and Read once each to ScannerA or ScannerB. Costs and usage are known, but ScannerB has no reported cumulative capacity.", "missing_one_capacity"),
    ("incomplete", "clarify", "boatbuilding", "Assign Hull and Mast to ShedA or ShedB, minimize stated euro costs. All four consumption values are absent. No zero usage default is authorized.", "missing_consumption_matrix"),
    ("incomplete", "clarify", "catering", "Choose a resource once for every catering task. The resource roster is supplied, but the complete list of mandatory tasks is missing.", "missing_task_roster"),
    ("incomplete", "clarify", "engraving", "Assign Cut and Etch to LaserA or LaserB. All costs are supplied as bare integers, but no cost unit is stated. Do not invent a currency or a unitless convention.", "missing_cost_unit"),
    ("invalid", "invalid", "coinmint", "The same authoritative specification assigns Strike on DieA a consumption of exactly 2 blank and exactly 7 blank simultaneously. Neither statement replaces the other.", "contradictory_pair_consumption"),
    ("invalid", "invalid", "fibreoptic", "SpoolA has cumulative capacity exactly 4 metre and exactly 11 metre simultaneously under the same measurement convention. Both statements are binding.", "contradictory_capacity"),
    ("invalid", "invalid", "railcleaning", "There are exactly three mandatory tasks Wash, Wipe, Sweep. The specification simultaneously states that there are exactly two mandatory tasks, with no optional task.", "contradictory_task_count"),
    ("invalid", "invalid", "pollenlab", "A nonnegative consumption is required for every task-resource pair, but the authoritative usage of Count on BenchA is -3 sample. This is a consumption, not a rebate.", "negative_consumption"),
    ("invalid", "invalid", "pianoworks", "The two different resources are both explicitly identified as RoomA, and their identifiers must remain unchanged. They have distinct capacities 2 and 8 minute. This violates unique resource identity.", "duplicate_resource_identifier"),
    ("unsupported", "unsupported", "ambulance", "Allocate emergency calls to vehicles, but also optimize the mandatory route visiting all patients with travel times between visits. Routing cannot be discarded.", "vehicle_routes"),
    ("unsupported", "unsupported", "telescope", "Assign exposures to instruments and compute start times: ExposureB must start after ExposureA finishes. This precedence schedule is mandatory.", "precedence_schedule"),
    ("unsupported", "unsupported", "dairy", "Assign batches to tanks while enforcing both cumulative litre capacity and a separate cumulative kilowatt-hour limit per tank. Both independent resource dimensions are mandatory.", "two_capacity_dimensions"),
    ("unsupported", "unsupported", "insurance", "Assign claims to reviewers but require each claim to be processed jointly by exactly two distinct reviewers. This is mandatory; one reviewer per claim is forbidden.", "two_resources_per_task"),
    ("unsupported", "unsupported", "hydropower", "Assign maintenance tasks to crews and minimize the maximum individual crew cost, rather than the sum of assignment costs. The minimax objective is mandatory.", "minimax_objective"),
]

DEV_SPECIALS = [
    ("ambiguous", "clarify", "speleology", "Assign Survey and Map once each to HutA or HutB, minimizing total euro cost. In task order Survey,Map, HutA costs 3,5 and HutB costs 4,2; every assignment consumes 1 slot. Two alternative scenarios are supplied, never simultaneous: Spring has cumulative capacities HutA=1 slot,HutB=1 slot; Autumn has HutA=2 slot,HutB=0 slot. The request does not identify which scenario to solve. Ask for the target scenario.", "unselected_alternative_scenario"),
    ("incomplete", "clarify", "numismatics", "Tasks Record and Appraise are mandatory, but no list of eligible resources is provided. Do not invent reviewers.", "missing_resource_roster"),
    ("incomplete", "clarify", "falconry", "Costs, capacities and consumptions are all integers. Capacity uses minute, but the consumption unit has not been supplied. No conversion convention is authorized.", "missing_usage_unit"),
    ("invalid", "invalid", "marquetry", "The fixed GAP input contract permits cumulative capacities only from 0 through 1000000000 slot. The authoritative cumulative capacity of SawA is exactly 1000000001 slot. This exceeds the permitted domain; no truncation, scaling, or replacement is authorized.", "capacity_outside_contract_domain"),
    ("unsupported", "unsupported", "caving", "Assign Survey and Map once each to CrewA or CrewB. Every assignment consumes 1 slot and each crew has cumulative capacity 2 slot. In task order Survey,Map, CrewA costs 3,5 euro and CrewB costs 4,2 euro. Minimize total euro cost, including a mandatory additional 7 euro interaction charge whenever Survey and Map use the same crew. The interaction charge cannot be discarded or represented as independent pair costs.", "nonadditive_assignment_interaction"),
]


def nonready_case(spec, split, index):
    challenge, disposition, family, issue, issue_kind = spec
    source = (f"Operational request for {family}. Use only supplied facts; do not repair, infer or discard a mandatory requirement. "
              + issue)
    return seal_case(dict(id=f"{split}_special_{index:02d}", split=split, family=family, category="special",
                          challenge=challenge, source=source, disposition=disposition, problem=None,
                          reference_optimum=None, reference_feasible_count=None, reference_optimum_count=None,
                          field_evidence={}, unit_equivalences={}, reference_issue=issue,
                          reference_annotation=dict(issue_kind=issue_kind, issue=issue,
                                                    expected_disposition=disposition)))


def build_corpus():
    cases = []
    simple_challenges = ["loose_capacity", "uniform_usage", "tight_heterogeneous", "rebates", "free_usage"]
    complex_challenges = ["greedy_trap", "tight_heterogeneous", "multiple_optima", "capacity_exclusions", "free_usage"]
    for index, row in enumerate(domains(DEV_DOMAINS)):
        category = "simple" if index < 8 else "combinatorial" if index < 16 else "language"
        challenges = complex_challenges if category == "combinatorial" else simple_challenges
        cases.append(ready_case(row, "dev", category, index, 7100 + index, challenges[index % len(challenges)]))
    for index, spec in enumerate(DEV_SPECIALS):
        cases.append(nonready_case(spec, "dev", index))
    for index in range(2):
        row = ["taxidermy" if index == 0 else "palaeontology", "Probe,Recheck,Certify",
               "RigJ,RigK" if index == 0 else "RigL,RigM,RigN", "sample"]
        cases.append(ready_case(row, "dev", "special", 5 + index, 7500 + index, "packing_infeasible"))
    for category, text in (("simple", SIMPLE_DOMAINS), ("combinatorial", COMBINATORIAL_DOMAINS), ("language", LANGUAGE_DOMAINS)):
        for index, row in enumerate(domains(text)):
            challenges = complex_challenges if category == "combinatorial" else simple_challenges
            seed = {"simple": 11000, "combinatorial": 22000, "language": 33000}[category] + index
            cases.append(ready_case(row, "test", category, index, seed, challenges[index % len(challenges)]))
    for index, spec in enumerate(SPECIALS):
        cases.append(nonready_case(spec, "test", index))
    for index, row in enumerate([
            ["railfreight", "Coal,Ore,Stone,Grain", "WagonA,WagonB,WagonC", "pallet"],
            ["vaccination", "DoseA,DoseB,DoseC", "NurseA,NurseB", "minute"],
            ["glassannealing", "PaneA,PaneB,PaneC,PaneD,PaneE", "OvenC,OvenD,OvenE", "rack"],
            ["circuitinspection", "Power,Signal,Ground,Clock", "ProbeA,ProbeB", "slot"],
            ["stonecutting", "SlabA,SlabB,SlabC,SlabD,SlabE,SlabF", "SawA,SawB,SawC,SawD", "unit"],
    ]):
        case = ready_case(row, "test", "special", 20 + index, 44000 + index, "packing_infeasible")
        case["challenge"] = "infeasible"
        cases.append(seal_case(case))
    data = dict(protocol_version=VERSION, generation_seed_policy="fixed independent integer seeds per case; no model observations",
                reference_policy="enumeration before any LLM observation; source round-trip; comparison with single-worker CP-SAT",
                family_definition="operational domain plus mathematical recipe and source syntax; development mathematical recipes and assignment-record syntax are absent from test; structural GAP contract is shared",
                cases=cases)
    data["content_sha256"] = sha(cases)
    return data


def audit(data, historical=None, compare_solver=True):
    """Reject corruption, renamed duplicates, split leakage, and wrong references."""
    if data.get("protocol_version") != VERSION or data.get("content_sha256") != sha(data["cases"]):
        raise ValueError("corpus version or content hash mismatch")
    cases = data["cases"]
    if Counter(c["split"] for c in cases) != {"dev": 30, "test": 100}:
        raise ValueError("expected 30 development and 100 test cases")
    if Counter(c["category"] for c in cases if c["split"] == "test") != dict.fromkeys(("simple", "combinatorial", "language", "special"), 25):
        raise ValueError("expected 25 test cases per category")
    if len({c["id"] for c in cases}) != len(cases):
        raise ValueError("duplicate case IDs")
    if len({c["family"] for c in cases}) != len(cases):
        raise ValueError("family overlap")
    recipes = {split: {c["generation_recipe"] for c in cases if c["split"] == split and c["disposition"] == "ready"}
               for split in ("dev", "test")}
    styles = {split: {c["source_style"] for c in cases if c["split"] == split and c["disposition"] == "ready"}
              for split in ("dev", "test")}
    if recipes["dev"] & recipes["test"] or styles["dev"] & styles["test"]:
        raise ValueError("mathematical recipe or source syntax leakage")
    historical_cases = (historical or {}).get("cases", [])
    source_seen = {sha(c["source"].strip()) for c in historical_cases}
    semantic_seen = {semantic_signature(c["problem"]) for c in historical_cases if c.get("problem") is not None}
    reference_checks, solver_checks = 0, 0
    cp_sat_comparisons = {}
    conservative_float_rejections = []
    for case in cases:
        identity = case["id"]
        if case["disposition"] not in ("ready", "clarify", "invalid", "unsupported"):
            raise ValueError(f"unknown disposition: {identity}")
        if not 0 < len(case["source"].strip()) <= 8000:
            raise ValueError(f"source size: {identity}")
        recomputed = seal_case({k: v for k, v in case.items() if k not in ("source_sha256", "problem_sha256", "semantic_sha256", "case_sha256")})
        if recomputed != case:
            raise ValueError(f"case hash mismatch: {identity}")
        source_hash, semantic_hash = sha(case["source"].strip()), case["semantic_sha256"]
        if source_hash in source_seen or semantic_hash in semantic_seen:
            raise ValueError(f"duplicate source or semantic formulation: {identity}")
        source_seen.add(source_hash)
        semantic_seen.add(semantic_hash)
        if case["disposition"] == "ready":
            problem = Gap.model_validate(case["problem"])
            if parse_source(case["source"]) != problem.model_dump():
                raise ValueError(f"source/reference mismatch: {identity}")
            if set(case["field_evidence"]) != set(Gap.model_fields) or any(
                    not quotes or any(not quote or quote not in case["source"] for quote in quotes)
                    for quotes in case["field_evidence"].values()):
                raise ValueError(f"invalid field evidence: {identity}")
            reference = enumerate_reference(problem.model_dump())
            if any(case[key] != reference[value] for key, value in (
                    ("reference_optimum", "optimum"), ("reference_feasible_count", "feasible_count"),
                    ("reference_optimum_count", "optimum_count"))):
                raise ValueError(f"incorrect reference: {identity}")
            reference_checks += 1
            if compare_solver:
                # Reconstruct integer objective from the Boolean witness. CP-SAT's
                # float objective can be 37.99999999999999 for an exact value 38.
                model, variables = build_model(problem)
                solver = cp_model.CpSolver()
                solver.parameters.max_time_in_seconds = 5.0
                solver.parameters.num_search_workers = 1
                solver.parameters.random_seed = 17
                status = solver.status_name(solver.solve(model))
                candidate = None
                if status == "OPTIMAL":
                    assignments = [dict(task=task, resource=resource)
                                   for j, task in enumerate(problem.tasks)
                                   for i, resource in enumerate(problem.resources) if solver.value(variables[i][j])]
                    owners = {a["task"]: a["resource"] for a in assignments}
                    objective = sum(problem.costs[problem.resources.index(owners[task])][j]
                                    for j, task in enumerate(problem.tasks))
                    candidate = dict(assignments=assignments, objective=objective)
                expected_status = "INFEASIBLE" if reference["optimum"] is None else "OPTIMAL"
                if status != expected_status or (candidate and candidate["objective"]) != reference["optimum"]:
                    raise ValueError(f"CP-SAT/reference mismatch: {identity}")
                if candidate and not verify(problem, candidate)["valid"]:
                    raise ValueError(f"CP-SAT witness invalid: {identity}")
                cp_sat_comparisons[identity] = dict(status=status, objective_exact=candidate["objective"] if candidate else None,
                                                    objective_raw=solver.objective_value if candidate else None,
                                                    best_bound_raw=solver.best_objective_bound)
                if candidate and not solver.objective_value.is_integer():
                    conservative_float_rejections.append(identity)
                solver_checks += 1
        elif (case["problem"] is not None or any(case[k] is not None for k in
              ("reference_optimum", "reference_feasible_count", "reference_optimum_count"))
              or not case.get("reference_issue") or case["reference_issue"] not in case["source"]
              or case["reference_annotation"]["expected_disposition"] != case["disposition"]):
            raise ValueError(f"incorrect non-ready reference: {identity}")
    return dict(valid=True, development_count=30, test_count=100, category_counts=dict(Counter(c["category"] for c in cases if c["split"] == "test")),
                reference_checks=reference_checks, source_roundtrips=reference_checks, cp_sat_checks=solver_checks,
                family_overlap=[], source_duplicates=[], semantic_duplicates=[], historical_overlap=[],
                development_recipes=sorted(recipes["dev"]), test_recipes=sorted(recipes["test"]),
                source_styles={split: sorted(values) for split, values in styles.items()},
                cp_sat_comparisons=cp_sat_comparisons,
                conservative_production_float_rejections=conservative_float_rejections,
                historical_cases_checked=len(historical_cases), content_sha256=data["content_sha256"],
                disposition_counts=dict(Counter(c["disposition"] for c in cases)),
                test_special_counts=dict(Counter(c["challenge"] for c in cases if c["split"] == "test" and c["category"] == "special")))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "audit"))
    parser.add_argument("--data", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--historical", type=Path, default=Path("data/benchmark.json"))
    parser.add_argument("--no-solver", action="store_true", help="audit arithmetic and text only; report zero CP-SAT checks")
    args = parser.parse_args()
    historical = load_json(args.historical.read_text(encoding="utf-8"))
    if args.command == "generate":
        data = build_corpus()
        report = audit(data, historical, compare_solver=not args.no_solver)
        with args.data.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
    else:
        report = audit(load_json(args.data.read_text(encoding="utf-8")), historical, compare_solver=not args.no_solver)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
