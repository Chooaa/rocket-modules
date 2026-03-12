#!/usr/bin/env python3
"""
Module Parser - 解析 SimTop.yaml 生成模块树形结构，提取指定顶层模块及其子模块代码。

用法:
    # 使用项目名称快速配置（自动从 $NOOP_HOME 复制文件、运行 svinst、生成 wrapper）:
    python3 module_parser.py --project-name dcache

    # 手动指定参数:
    python3 module_parser.py --root-module NonBlockingDCache [--yaml SimTop.yaml] [--sv SimTop.sv]
                             [--output modules.sv] [--tree] [--module-tree] [--extract]

    # 仅运行 svinst 生成 YAML:
    python3 module_parser.py --svinst-parse SimTop.sv
"""

import argparse
import glob as glob_mod
import os
import re
import shutil
import subprocess
import sys
from collections import OrderedDict

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RTL_DIR = os.path.join(SCRIPT_DIR, "rtl")

NOOP_HOME = os.environ.get(
    "NOOP_HOME",
    os.path.dirname(SCRIPT_DIR)
)
TMP_DIR = os.path.join(NOOP_HOME, "tmp")

# ============================================================
# 0. 项目默认配置 & svinst 工具 & 项目初始化
# ============================================================

PROJECT_CONFIGS = {
    "rocket_dcache": {
        "root_modules": ["NonBlockingDCache"],
        # "root_modules": ["DCache"],
        "description": "DCache (NonBlockingDCache) 模块",
    },
    "rocket_fpu": {
        "root_modules": ["FPU"],
        "description": "FPU 模块",
    },
    "rocket_frontend": {
        "root_modules": ["Frontend"],
        "description": "Frontend 模块",
    },
    "boom_dcache": {
        "root_modules": ["BoomNonBlockingDCache"],
        "description": "DCache (BoomNonBlockingDCache) 模块",
    },
}


def find_svinst():
    """查找 svinst 可执行文件，优先使用脚本同目录下的 svinst。"""
    local_svinst = os.path.join(SCRIPT_DIR, "svinst")
    if os.path.isfile(local_svinst) and os.access(local_svinst, os.X_OK):
        return local_svinst
    if shutil.which("svinst"):
        return shutil.which("svinst")
    return None


def run_svinst(sv_path, yaml_output=None, inc_dirs=None, define_macros=None):
    """使用 svinst 解析 Verilog/SystemVerilog 文件，生成对应的 YAML 文件。

    Args:
        sv_path: 要解析的 SV 文件路径
        yaml_output: YAML 输出路径，为 None 则自动在 SV 文件同目录生成 <stem>.yaml
        inc_dirs: include 目录列表
        define_macros: 宏定义列表
    Returns:
        生成的 YAML 文件路径，失败返回 None
    """
    svinst = find_svinst()
    if svinst is None:
        print("[ERROR] 找不到 svinst 可执行文件（脚本同目录或 PATH 中均未找到）", file=sys.stderr)
        return None

    if not os.path.isfile(sv_path):
        print(f"[ERROR] SV 文件不存在: {sv_path}", file=sys.stderr)
        return None

    if yaml_output is None:
        stem = os.path.splitext(os.path.basename(sv_path))[0]
        yaml_output = os.path.join(os.path.dirname(sv_path), f"{stem}.yaml")

    cmd = [svinst, sv_path]
    # for m in (define_macros or []):
    #     cmd.extend(["-D", m])

    print(f"[INFO] 运行 svinst: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            print(f"[ERROR] svinst 失败 (exit={result.returncode}): {result.stderr}", file=sys.stderr)
            return None
        os.makedirs(os.path.dirname(yaml_output) or ".", exist_ok=True)
        with open(yaml_output, "w") as f:
            f.write(result.stdout)
        print(f"[INFO] 已生成 YAML: {yaml_output}")
        return yaml_output
    except Exception as e:
        print(f"[ERROR] 运行 svinst 出错: {e}", file=sys.stderr)
        return None


def setup_project(project_name, include_generic=False, renumber=True, insert_initial=False):
    """根据 project_name 完成整个项目的 RTL 处理流程。

    完整流程:
      1. 运行 svinst 解析 $NOOP_HOME/build/rtl/SimTop.sv 生成 YAML
      2. 解析 YAML 获取模块层次，收集目标模块
      3. 从源 SimTop.sv 中提取目标模块代码
      4. 生成顶层 fuzz wrapper 并追加提取的模块代码 → modules/rtl/SimTop.sv
      5. 复制并过滤 firrtl-cover 文件 → modules/rtl/
      6. 复制 GEN_* 覆盖点模块文件 → modules/rtl/
      7. 生成形式验证顶层 FormalTop.sv → modules/FormalTop.sv

    Returns:
        dict with keys: root_modules, sv_path, yaml_path, rtl_dir, modules, target_mods
        失败返回 None
    """
    config = PROJECT_CONFIGS.get(project_name)
    if config is None:
        print(f"[ERROR] 未知项目: {project_name}", file=sys.stderr)
        print(f"[INFO] 支持的项目: {', '.join(PROJECT_CONFIGS.keys())}", file=sys.stderr)
        return None

    root_modules = config["root_modules"]
    noop_home = os.environ.get("NOOP_HOME", NOOP_HOME)
    build_rtl = os.path.join(noop_home, "build", "rtl")
    generated_src = os.path.join(noop_home, "build", "generated-src")

    src_simtop = os.path.join(build_rtl, "SimTop.sv")
    if not os.path.isfile(src_simtop):
        print(f"[ERROR] 未找到源文件: {src_simtop}", file=sys.stderr)
        return None

    shutil.rmtree(RTL_DIR, ignore_errors=True)
    os.makedirs(RTL_DIR, exist_ok=True)
    print(f"[INFO] RTL 目标目录: {RTL_DIR}")

    # ---- Step 1: 运行 svinst 解析源 SimTop.sv 生成 YAML ----
    yaml_path = os.path.join(build_rtl, "SimTop.yaml")
    print(f"\n--- Step 1: 运行 svinst 解析 {src_simtop} ---")
    svinst_result = run_svinst(src_simtop, yaml_path)
    if svinst_result is None:
        print("[WARNING] svinst 解析失败，尝试使用已有 YAML 继续", file=sys.stderr)
        if not os.path.isfile(yaml_path):
            return None

    # ---- Step 2: 解析 YAML，收集目标模块 ----
    print(f"\n--- Step 2: 解析 YAML 并收集目标模块 ---")
    modules = parse_yaml(yaml_path)
    print(f"[INFO] 共解析 {len(modules)} 个模块定义")
    target_mods = get_target_modules(modules, root_modules, include_generic=include_generic)
    print(f"[INFO] 目标模块 (root: {', '.join(root_modules)}): 共 {len(target_mods)} 个")

    # ---- Step 3: 从源 SimTop.sv 提取目标模块代码 ----
    # Fuzz 版本带 initial 语句，Formal 版本不插入 initial
    print(f"\n--- Step 3: 提取目标模块代码 ---")
    extracted_text_fuzz = extract_modules(src_simtop, target_mods,
                                          renumber=renumber, insert_initial=insert_initial)
    # extracted_text_formal = extract_modules(src_simtop, target_mods,
                                            # renumber=renumber, insert_initial=insert_initial)

    # ---- Step 4: 生成顶层 fuzz wrapper (SimTop.sv) 并追加提取的模块代码 ----
    wrapper_sv = os.path.join(RTL_DIR, "SimTop.sv")
    fuzz_top = root_modules[0]
    print(f"\n--- Step 4: 生成顶层包裹 SimTop.sv (包裹 {fuzz_top}) ---")
    generate_fuzz_wrapper(src_simtop, fuzz_top, wrapper_sv, insert_initial=insert_initial)

    with open(wrapper_sv, "a") as f:
        f.write("\n")
        f.write(extracted_text_fuzz)
    print(f"[INFO] 已将提取的模块代码追加到 {wrapper_sv}")

    # ---- Step 5: 复制并过滤 firrtl-cover 文件 ----
    print(f"\n--- Step 5: 处理 firrtl-cover 文件 ---")
    if os.path.isdir(generated_src):
        cover_files = glob_mod.glob(os.path.join(generated_src, "firrtl-cover*"))
        for cf in cover_files:
            dst = os.path.join(RTL_DIR, os.path.basename(cf))
            shutil.copy2(cf, dst)
        if cover_files:
            print(f"[INFO] 已复制 {len(cover_files)} 个 firrtl-cover* 文件到 {RTL_DIR}")

        cover_cpp_src = os.path.join(generated_src, "firrtl-cover.cpp")
        if os.path.isfile(cover_cpp_src):
            cover_cpp_filtered = os.path.join(RTL_DIR, "firrtl-cover.cpp")
            filter_cover_cpp(cover_cpp_src, cover_cpp_filtered, target_mods)
        else:
            print("[WARNING] firrtl-cover.cpp 不存在，跳过过滤", file=sys.stderr)
    else:
        print(f"[WARNING] generated-src 目录不存在: {generated_src}", file=sys.stderr)

    # ---- Step 6: 复制 GEN_* 覆盖点模块文件 ----
    print(f"\n--- Step 6: 复制 GEN_* 文件 ---")
    gen_files = glob_mod.glob(os.path.join(build_rtl, "GEN_*"))
    for gf in gen_files:
        dst = os.path.join(RTL_DIR, os.path.basename(gf))
        shutil.copy2(gf, dst)
    if gen_files:
        print(f"[INFO] 已复制 {len(gen_files)} 个 GEN_* 文件到 {RTL_DIR}")
    else:
        print("[INFO] 未找到 GEN_* 文件")

    # ---- Step 7: 生成形式验证顶层 FormalTop.sv (放在 SCRIPT_DIR 而非 RTL_DIR) ----
    formal_sv = os.path.join(SCRIPT_DIR, "FormalTop.sv")
    print(f"\n--- Step 7: 生成形式验证顶层 FormalTop.sv (包裹 {fuzz_top}) ---")
    generate_formal_wrapper(src_simtop, fuzz_top, formal_sv, insert_initial=insert_initial)

    with open(formal_sv, "a") as f:
        f.write("\n")
        f.write(extracted_text_fuzz)
    print(f"[INFO] 已将提取的模块代码追加到 {formal_sv}")

    print(f"\n{'=' * 70}")
    print(f"[INFO] 项目 {project_name} 初始化完成")
    print(f"  - SimTop (wrapper + 模块): {wrapper_sv}")
    print(f"  - FormalTop:               {formal_sv}")
    cover_filtered_path = os.path.join(RTL_DIR, "firrtl-cover-filtered.cpp")
    if os.path.isfile(cover_filtered_path):
        print(f"  - 过滤后 cover:            {cover_filtered_path}")
    print(f"  - GEN_* 文件:              {len(gen_files)} 个")
    print(f"{'=' * 70}")

    return {
        "root_modules": root_modules,
        "sv_path": wrapper_sv,
        "yaml_path": yaml_path,
        "rtl_dir": RTL_DIR,
        "modules": modules,
        "target_mods": target_mods,
        "description": config["description"],
        "processed": True,
    }


# ============================================================
# 1. 解析 YAML，构建模块定义与实例化树
# ============================================================

class ModuleDef:
    """表示一个模块定义（mod_name）及其实例化的子模块列表。"""

    def __init__(self, mod_name):
        self.mod_name = mod_name
        # list of (inst_name, mod_name) 表示实例化关系
        self.instances = []

    def add_instance(self, inst_name, child_mod_name):
        self.instances.append((inst_name, child_mod_name))

    def __repr__(self):
        return f"ModuleDef({self.mod_name}, insts={len(self.instances)})"


def parse_yaml(yaml_path):
    """解析 SimTop.yaml，返回 {mod_name: ModuleDef} 的有序字典。"""
    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)

    modules = OrderedDict()

    for file_entry in data.get("files", []):
        for mod_def in file_entry.get("defs", []):
            mod_name = mod_def["mod_name"]
            m = ModuleDef(mod_name)
            for inst in mod_def.get("insts") or []:
                child_mod = inst["mod_name"]
                child_inst = inst.get("inst_name", child_mod)
                m.add_instance(child_inst, child_mod)
            modules[mod_name] = m

    return modules


# ============================================================
# 2. 打印树形结构
# ============================================================

def print_tree(modules, root_name, indent="", visited=None, max_depth=None, depth=0,
               skip_generic=True):
    """递归打印以 root_name 为根的模块实例化树。

    Args:
        skip_generic: 是否跳过 GEN_w* 等生成的辅助模块以简化输出。
        max_depth: 最大递归深度，None 表示不限。
    """
    if visited is None:
        visited = set()

    if max_depth is not None and depth > max_depth:
        return

    mod = modules.get(root_name)
    if mod is None:
        return

    instances = mod.instances
    if skip_generic:
        instances = [(iname, mname) for iname, mname in instances
                     if not mname.startswith("GEN_w")]

    for i, (inst_name, child_mod) in enumerate(instances):
        is_last = (i == len(instances) - 1)
        connector = "└── " if is_last else "├── "
        print(f"{indent}{connector}{child_mod} ({inst_name})", end="")

        if child_mod in visited:
            print("  [*循环引用*]")
            continue
        print()

        next_indent = indent + ("    " if is_last else "│   ")
        visited.add(child_mod)
        print_tree(modules, child_mod, next_indent, visited, max_depth, depth + 1,
                   skip_generic)
        visited.discard(child_mod)


# ============================================================
# 3. 提取指定顶层模块的子模块名集合（递归收集）
# ============================================================


def collect_submodules(modules, root_name, result=None, skip_generic=True):
    """递归收集 root_name 下所有子模块名（含自身）。"""
    if result is None:
        result = set()
    if root_name in result:
        return result
    result.add(root_name)
    mod = modules.get(root_name)
    if mod is None:
        return result
    for inst_name, child_mod in mod.instances:
        if skip_generic and child_mod.startswith("GEN_w"):
            continue
        if inst_name.startswith("difftest_"):
            print(f"[INFO] collect_submodules: 跳过 difftest 实例化 {inst_name}")
            continue
        collect_submodules(modules, child_mod, result, skip_generic)
    return result


def get_target_modules(modules, root_modules, include_generic=False):
    """获取指定顶层模块及其所有子模块名集合。"""
    target_mods = set()
    for root in root_modules:
        collect_submodules(modules, root, target_mods, skip_generic=not include_generic)
    return target_mods


# ============================================================
# 4. 从 SimTop.sv 中提取指定模块的代码
# ============================================================

def find_module_ranges(sv_path):
    """扫描 SV 文件，返回 {mod_name: (start_line, end_line)} 的字典（行号从0开始）。"""
    module_ranges = {}
    module_pattern = re.compile(r"^module\s+(\w+)")

    with open(sv_path, "r") as f:
        lines = f.readlines()

    current_module = None
    start_line = None

    for i, line in enumerate(lines):
        m = module_pattern.match(line)
        if m:
            current_module = m.group(1)
            start_line = i
        elif line.strip() == "endmodule" and current_module is not None:
            module_ranges[current_module] = (start_line, i)
            current_module = None
            start_line = None

    return module_ranges, lines

toggle_cover_id = 0
line_cover_id = 0
mux_cover_id = 0
control_cover_id = 0

def _cover_increment(kind, width):
    """计算一个 GEN_wN_<kind> 实例占用的 cover point 数量。

    - toggle / line / mux: valid 每一位独立触发，占 width 个点
    - control w1: 仅在 valid==1 时触发，占 1 个点
    - control w2+: COVER_INDEX + valid，valid 范围 0~2^width-1，占 2^width 个点
    """
    if kind == "control":
        return 1 if width == 1 else (1 << width)
    return width


def renumber_cover_points(module_text):
    """对单个模块内的覆盖点（toggle_N, line_N, mux_N, control_N）重新编号。

    处理的结构包括:
      - wire/reg 声明: wire mux_N_clock; reg mux_N_valid_reg;
      - 实例化: GEN_wW_kind #(.COVER_INDEX(N)) kind_N (
      - 端口连接: .clock(mux_N_clock),
      - assign 语句: assign mux_N_valid = ...;
      - always 块: mux_N_valid_reg <= ...;
      - initial 块: mux_N_valid_reg = _RAND_...;

    cover ID 增量规则:
      - toggle / line / mux: 每实例 +width（valid 每位一个点）
      - control w1: +1
      - control w2+: +2^width

    Returns:
        (renumbered_text, cover_counts, rename_map)
        cover_counts: dict with keys "toggle", "line", "mux", "control"
                      值为该类型的总 cover point 数量
    """
    cover_kinds = ("toggle", "line", "mux", "control")
    kind_alt = "|".join(cover_kinds)
    inst_alt = "|".join(rf"{k}_\d+" for k in cover_kinds)

    inst_pattern = re.compile(
        rf'GEN_w(\d+)_({kind_alt})\s+#\(\.COVER_INDEX\(\d+\)\)\s+({inst_alt})\s+\('
    )

    # (kind, inst_name, width) 保留出现顺序
    old_entries = {k: [] for k in cover_kinds}
    seen = set()

    for m in inst_pattern.finditer(module_text):
        width = int(m.group(1))
        kind = m.group(2)
        inst_name = m.group(3)
        if inst_name not in seen:
            seen.add(inst_name)
            old_entries[kind].append((inst_name, width))

    rename_map = {}
    global toggle_cover_id, line_cover_id, mux_cover_id, control_cover_id
    cover_id_map = {
        "toggle": toggle_cover_id,
        "line": line_cover_id,
        "mux": mux_cover_id,
        "control": control_cover_id,
    }

    cover_counts = {}
    for kind in cover_kinds:
        cid = cover_id_map[kind]
        total_points = 0
        for old_name, width in old_entries[kind]:
            rename_map[old_name] = f"{kind}_{cid}"
            inc = _cover_increment(kind, width)
            cid += inc
            total_points += inc
        cover_counts[kind] = total_points
        cover_id_map[kind] = cid

    toggle_cover_id = cover_id_map["toggle"]
    line_cover_id = cover_id_map["line"]
    mux_cover_id = cover_id_map["mux"]
    control_cover_id = cover_id_map["control"]

    if not rename_map:
        return module_text, {k: 0 for k in cover_kinds}, {}

    sorted_old = sorted(rename_map.keys(), key=len, reverse=True)

    old_pattern = re.compile(
        r'\b(' + '|'.join(re.escape(name) for name in sorted_old) + r')(?!\d)'
    )

    def replace_func(match):
        return rename_map[match.group(1)]

    result = old_pattern.sub(replace_func, module_text)

    def update_cover_index(m):
        gen_type = m.group(1)
        inst_name = m.group(2)
        new_idx = inst_name.split("_", 1)[1]
        return f'{gen_type} #(.COVER_INDEX({new_idx})) {inst_name} ('

    cover_idx_pattern = re.compile(
        rf'(GEN_w\d+_(?:{kind_alt}))\s+#\(\.COVER_INDEX\(\d+\)\)\s+({inst_alt})\s+\('
    )

    result = cover_idx_pattern.sub(update_cover_index, result)

    return result, cover_counts, rename_map


def insert_reg_initial(module_text):
    """为模块中的所有寄存器插入 initial begin ... end 赋值初始化语句。

    对于普通寄存器 reg [N:0] foo:
        foo = '0;

    对于存储器数组 reg [N:0] foo [0:M]:
        foo[0] = '0;
        foo[1] = '0;
        ...

    跳过 _RAND_* 辅助寄存器。
    """
    # 匹配普通寄存器: reg  name;  或  reg [N:M] name;
    scalar_reg_pat = re.compile(
        r'^\s+reg\s+(?:\[\d+:\d+\]\s+)?(\w+)\s*;', re.MULTILINE
    )
    # 匹配存储器数组: reg [N:M] name [0:K];
    mem_reg_pat = re.compile(
        r'^\s+reg\s+(?:\[\d+:\d+\]\s+)?(\w+)\s+\[(\d+):(\d+)\]\s*;', re.MULTILINE
    )

    scalar_regs = []
    mem_regs = []

    for m in scalar_reg_pat.finditer(module_text):
        name = m.group(1)
        if name.startswith("_RAND_"):
            continue
        # 排除存储器数组（它们会被 mem_reg_pat 单独处理）
        # 检查匹配位置后面是否紧跟 [0:N] 维度声明
        after = module_text[m.end():m.end() + 5].strip()
        # scalar_reg_pat 的 ; 已经匹配到行尾，所以不会误匹配 mem
        scalar_regs.append(name)

    for m in mem_reg_pat.finditer(module_text):
        name = m.group(1)
        lo = int(m.group(2))
        hi = int(m.group(3))
        if name.startswith("_RAND_"):
            continue
        mem_regs.append((name, lo, hi))

    # 从 scalar_regs 中去掉实际是 mem 的（名字重复）
    mem_names = {name for name, _, _ in mem_regs}
    scalar_regs = [r for r in scalar_regs if r not in mem_names]

    if not scalar_regs and not mem_regs:
        return module_text, 0

    init_lines = []
    if scalar_regs or mem_regs:
        init_lines.append("  initial begin")
        for name in scalar_regs:
            init_lines.append(f"    {name} = '0;")
        for name, lo, hi in mem_regs:
            for i in range(lo, hi + 1):
                init_lines.append(f"    {name}[{i}] = '0;")
        init_lines.append("  end")

    init_block = "\n".join(init_lines) + "\n"

    # 插入在 endmodule 之前
    module_text = module_text.rstrip()
    if module_text.endswith("endmodule"):
        module_text = module_text[:-len("endmodule")] + init_block + "endmodule\n"
    else:
        module_text += "\n" + init_block

    total = len(scalar_regs) + len(mem_regs)
    return module_text, total


# 实例化匹配: "  ModuleName inst_name (" 或 "  ModuleName #(...) inst_name ("
_INST_HEAD_RE = re.compile(r'^\s+(\w+)\s+(?:#\(.*?\)\s+)?(\w+)\s*\(')


def strip_difftest_instances(module_text):
    """从模块文本中移除 difftest 相关的实例化块（实例名以 difftest_ 开头）。

    仅移除实例化块（ModuleName difftest_* ( ... );），保留 difftest 相关的
    wire 声明和 assign 语句。被实例化的模块定义（如 DelayReg）由
    collect_submodules 排除，不会被提取。
    """
    lines = module_text.splitlines(keepends=True)
    out = []
    i = 0
    removed = 0
    in_difftest_inst = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if in_difftest_inst:
            removed += 1
            if stripped.endswith(");"):
                in_difftest_inst = False
            i += 1
            continue

        m = _INST_HEAD_RE.match(line)
        if m:
            inst_name = m.group(2)
            if inst_name.startswith("difftest_"):
                in_difftest_inst = True
                removed += 1
                if stripped.endswith(");"):
                    in_difftest_inst = False
                i += 1
                continue

        out.append(line)
        i += 1

    if removed:
        print(f"[INFO] strip_difftest: 移除 {removed} 行 difftest 实例化代码")
    return "".join(out)


def extract_modules(sv_path, mod_names, output_path=None, renumber=True, insert_initial=True):
    """从 SV 文件中提取指定模块的代码。

    Args:
        sv_path: SimTop.sv 路径
        mod_names: 要提取的模块名集合
        output_path: 输出文件路径，为 None 则输出到 stdout
        renumber: 是否对覆盖点重新编号
        insert_initial: 是否为寄存器插入 initial 语句块
    """
    module_ranges, lines = find_module_ranges(sv_path)

    # 按照在文件中的出现顺序排列
    sorted_mods = sorted(
        [(name, rng) for name, rng in module_ranges.items() if name in mod_names],
        key=lambda x: x[1][0]
    )

    missing = mod_names - set(module_ranges.keys())
    if missing:
        print(f"[WARNING] 以下模块在 SV 文件中未找到定义: {missing}", file=sys.stderr)

    output_lines = []
    cover_kinds = ("toggle", "line", "mux", "control")

    output_lines.append(f"// ===== 提取的模块代码 =====\n")
    output_lines.append(f"// 共 {len(sorted_mods)} 个模块\n")
    output_lines.append(f"// 模块列表: {', '.join(name for name, _ in sorted_mods)}\n")
    if renumber:
        output_lines.append(f"// 覆盖点已重新编号 (toggle/line/mux/control: 各自 0-based)\n")
    output_lines.append(f"//\n\n")

    totals = {k: 0 for k in cover_kinds}
    total_init_regs = 0

    for mod_name, (start, end) in sorted_mods:
        mod_text = "".join(lines[start:end + 1])

        mod_text = strip_difftest_instances(mod_text)

        if renumber:
            mod_text, counts, rmap = renumber_cover_points(mod_text)
            for k in cover_kinds:
                totals[k] += counts[k]

        if insert_initial:
            mod_text, init_cnt = insert_reg_initial(mod_text)
            total_init_regs += init_cnt

        output_lines.append(f"// {'=' * 60}\n")
        output_lines.append(f"// Module: {mod_name} (lines {start + 1}-{end + 1})\n")
        if renumber and any(counts[k] for k in cover_kinds):
            parts = ", ".join(f"{counts[k]} {k}" for k in cover_kinds if counts[k])
            output_lines.append(f"// Cover points: {parts}\n")
        output_lines.append(f"// {'=' * 60}\n")
        output_lines.append(mod_text)
        output_lines.append("\n\n")

    text = "".join(output_lines)

    total_lines = sum(e - s + 1 for _, (s, e) in sorted_mods)
    print(f"[INFO] 已提取 {len(sorted_mods)} 个模块 ({total_lines} 行)")
    if insert_initial:
        print(f"[INFO] 寄存器初始化: 共 {total_init_regs} 个寄存器插入 initial 语句")
    if renumber:
        summary = ", ".join(
            f"{totals[k]} {k} (0-{max(totals[k]-1,0)})" for k in cover_kinds
        )
        print(f"[INFO] 覆盖点重新编号: {summary}")

    if output_path:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"[INFO] 输出到: {output_path}")

    return text

# ============================================================
# 5. 过滤 firrtl-cover.cpp 中不属于指定模块的覆盖点
# ============================================================


def filter_cover_cpp(input_path, output_path, allowed_modules):
    """过滤 firrtl-cover.cpp，只保留属于 allowed_modules 的覆盖点。

    解析四个 NAMES 数组 (line, toggle, mux, control)，过滤条目并更新
    结构体大小和 firrtl_cover 数组中的计数。
    """
    with open(input_path, "r") as f:
        content = f.read()

    array_pattern = re.compile(
        r'static const char \*(\w+)_NAMES\[\] = \{\n(.*?)\n\};',
        re.DOTALL,
    )
    entry_pattern = re.compile(r'"([^"]+)"')

    array_info = {}  # kind -> (original_entries, filtered_entries)

    def filter_array(m):
        kind = m.group(1)          # "line", "toggle", "mux", "control"
        body = m.group(2)
        entries = entry_pattern.findall(body)
        filtered = [e for e in entries if e.split(".")[0] in allowed_modules]
        array_info[kind] = (len(entries), len(filtered))

        if not filtered:
            inner = ""
        else:
            inner = "\n".join(f'  "{e}",' for e in filtered)
        return f"static const char *{kind}_NAMES[] = {{\n{inner}\n}};"

    result = array_pattern.sub(filter_array, content)

    # 更新 CoverPoints 结构体中的数组大小
    for kind, (orig, filt) in array_info.items():
        result = re.sub(
            rf'uint8_t {kind}\[{orig}\]',
            f'uint8_t {kind}[{filt}]',
            result,
        )

    # 更新 firrtl_cover 数组中的大小
    for kind, (orig, filt) in array_info.items():
        result = re.sub(
            rf'{kind}, {orig}UL',
            f'{kind}, {filt}UL',
            result,
        )

    # 在 v_cover_control 中插入 new_points_covered 通知逻辑
    new_cover_snippet = (
        "    if (coverPoints.control[index] == 0) {\n"
        "        extern bool new_points_covered;\n"
        "        new_points_covered = true;\n"
        "    }\n"
        "    extern uint8_t *acc_cover;\n"
        "    if (acc_cover[index] == 0) {\n"
        "        extern uint64_t acc_covered_num;\n"
        "        acc_covered_num++;\n"
        "    }\n"
        "    acc_cover[index] = 1;\n"
    )
    result = re.sub(
        r'(extern "C" void v_cover_control\(uint64_t index\) \{\n)'
        r'(\s*coverPoints\.control\[index\] = 1;)',
        rf'\g<1>{new_cover_snippet}\2',
        result,
    )

    with open(output_path, "w") as f:
        f.write(result)

    print(f"[INFO] 已过滤覆盖点 -> {output_path}")
    for kind in ("line", "toggle", "mux", "control"):
        if kind in array_info:
            orig, filt = array_info[kind]
            print(f"  {kind}: {orig} -> {filt} (去除 {orig - filt})")



# ============================================================
# 6. 生成 Fuzz 包裹模块
# ============================================================

def parse_module_ports(sv_path, mod_name):
    """解析 SV 文件中指定模块的端口声明。

    Returns:
        list of (direction, width, port_name)
        direction: "input" 或 "output"
        width: 位宽整数（1 表示标量）
        port_name: 端口名字符串
    """
    module_ranges, lines = find_module_ranges(sv_path)
    if mod_name not in module_ranges:
        raise ValueError(f"模块 {mod_name} 在 {sv_path} 中未找到")

    start, end = module_ranges[mod_name]

    # 收集从 module 声明到 ); 之间的端口行
    port_lines = []
    in_ports = False
    for i in range(start, end + 1):
        line = lines[i]
        if i == start:
            in_ports = True
            continue
        if in_ports:
            port_lines.append(line)
            if line.strip().startswith(");"):
                break

    port_text = "".join(port_lines)

    # 匹配端口声明: input/output [N:M] name
    port_pat = re.compile(
        r'(input|output)\s+(?:\[(\d+):(\d+)\]\s+)?(\w+)'
    )

    ports = []
    for m in port_pat.finditer(port_text):
        direction = m.group(1)
        hi = int(m.group(2)) if m.group(2) is not None else 0
        lo = int(m.group(3)) if m.group(3) is not None else 0
        width = hi - lo + 1
        name = m.group(4)
        ports.append((direction, width, name))

    return ports


def generate_fuzz_wrapper(sv_path, mod_name, output_path=None, insert_initial=False):
    """生成 Fuzz 包裹模块。

    原顶层模块的所有 input（除 clock 和 reset）均从 reg_input 输入，
    reg_input 通过 fuzz_get_input(len) 获取。

    Args:
        sv_path: SV 文件路径
        mod_name: 要包裹的顶层模块名
        output_path: 输出文件路径，为 None 则返回字符串
        insert_initial: 是否为 reg_input 插入 initial 语句块
    Returns:
        生成的 wrapper 模块代码字符串
    """
    ports = parse_module_ports(sv_path, mod_name)

    # 分离 input（排除 clock/reset）和 output
    skip_inputs = {"clock", "reset"}
    fuzz_inputs = [(d, w, n) for d, w, n in ports
                   if d == "input" and n not in skip_inputs]
    all_ports = ports

    # 计算总输入位宽
    total_width = sum(w for _, w, _ in fuzz_inputs)

    wrapper_name = f"SimTop"

    n_bytes = (total_width + 7) // 8
    padded_width = n_bytes * 8

    lines = []
    lines.append(f"// DPI-C import: fuzz_get_byte 从 C++ fuzz buffer 逐字节获取输入")
    lines.append(f"import \"DPI-C\" function byte fuzz_get_byte();")
    lines.append(f"")
    lines.append(f"module {wrapper_name}(")
    lines.append(f"  input clock,")
    lines.append(f"  input reset")
    lines.append(f");")
    lines.append(f"")
    lines.append(f"  // 总 fuzz 输入位宽: {total_width}, 对齐到字节: {padded_width} ({n_bytes} bytes)")
    lines.append(f"  reg [{padded_width - 1}:0] reg_input;")
    lines.append(f"  integer _byte_i;")
    lines.append(f"  always @(posedge clock) begin")
    lines.append(f"    if (reset) begin")
    lines.append(f"      reg_input <= {padded_width}'b0;")
    lines.append(f"    end else begin")
    lines.append(f"      for (_byte_i = 0; _byte_i < {n_bytes}; _byte_i = _byte_i + 1) begin")
    lines.append(f"        reg_input[_byte_i*8 +: 8] <= fuzz_get_byte();")
    lines.append(f"      end")
    lines.append(f"    end")
    lines.append(f"  end")
    if insert_initial:
        lines.append(f"  initial begin")
        lines.append(f"    reg_input = '0;")
        lines.append(f"  end")
    lines.append(f"")

    # 为每个 fuzz input 声明 wire 并从 reg_input 中切片赋值
    bit_offset = 0
    lines.append(f"  // fuzz input 信号声明与赋值")
    for _, width, name in fuzz_inputs:
        if width == 1:
            lines.append(f"  wire {name};")
            lines.append(f"  assign {name} = reg_input[{bit_offset}];")
        else:
            lines.append(f"  wire [{width - 1}:0] {name};")
            lines.append(f"  assign {name} = reg_input[{bit_offset + width - 1}:{bit_offset}];")
        bit_offset += width
    
    # 将input按顺序保存到文件
    # fuzz_input_file = os.path.join(SCRIPT_DIR, "fuzz_inputs.txt")
    # with open(fuzz_input_file, "w") as f:
    #     for _, width, name in fuzz_inputs:
    #         f.write(f"{name}\n")

    # 为 output 声明 wire
    outputs = [(d, w, n) for d, w, n in all_ports if d == "output"]
    if outputs:
        lines.append(f"")
        lines.append(f"  // output 信号声明")
        for _, width, name in outputs:
            if width == 1:
                lines.append(f"  wire {name};")
            else:
                lines.append(f"  wire [{width - 1}:0] {name};")

    # 实例化原模块
    lines.append(f"")
    lines.append(f"  // 实例化原顶层模块")
    lines.append(f"  {mod_name} dut (")
    port_conns = []
    for _, _, name in all_ports:
        port_conns.append(f"    .{name}({name})")
    lines.append(",\n".join(port_conns))
    lines.append(f"  );")
    lines.append(f"")
    lines.append(f"endmodule")

    text = "\n".join(lines) + "\n"

    if output_path:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"[INFO] 已生成 fuzz wrapper 模块 -> {output_path}")
        print(f"[INFO] 包裹模块: {mod_name}, fuzz 输入位宽: {total_width}")
    else:
        return text


def generate_formal_wrapper(sv_path, mod_name, output_path=None, insert_initial=False):
    """生成形式验证顶层包裹模块 FormalTop.sv。

    采用与 generate_fuzz_wrapper 相同的 reg_input 切片结构:
      - 所有 DUT input（除 clock/reset）打包到一个宽 reg_input 中
      - 各 DUT input 从 reg_input 中切片赋值
    不同之处:
      - reg_input 的数据来源是 FormalTop 的 input 端口（而非 fuzz_get_byte）
      - 使用 (* gclk *) 全局时钟和 reg_reset 复位逻辑

    Args:
        sv_path: SV 文件路径（用于解析 mod_name 的端口）
        mod_name: 要包裹的 DUT 模块名
        output_path: 输出文件路径，为 None 则返回字符串
        insert_initial: 是否为 reg_input 插入 initial 语句块
    Returns:
        生成的 FormalTop 模块代码字符串（当 output_path 为 None 时）
    """
    ports = parse_module_ports(sv_path, mod_name)

    skip_inputs = {"clock", "reset"}
    fuzz_inputs = [(d, w, n) for d, w, n in ports
                   if d == "input" and n not in skip_inputs]
    all_ports = ports

    total_width = sum(w for _, w, _ in fuzz_inputs)
    n_bytes = (total_width + 7) // 8
    padded_width = n_bytes * 8

    lines = []
    lines.append(f"`define SYNTHESIS")
    lines.append(f"module FormalTop (")
    lines.append(f"  input [{padded_width - 1}:0] formal_input")
    lines.append(f");")
    lines.append(f"")

    # 全局时钟 & 复位
    lines.append(f"  (* gclk *) wire glb_clk;")
    lines.append(f"  wire clock;")
    lines.append(f"  wire reset;")
    lines.append(f"")
    lines.append(f"  reg reg_reset = 1'b1;")
    lines.append(f"  always @(posedge glb_clk) begin")
    lines.append(f"    if (reg_reset) begin")
    lines.append(f"      reg_reset <= 1'b0;")
    lines.append(f"    end")
    lines.append(f"  end")
    lines.append(f"")
    lines.append(f"  assign clock = glb_clk;")
    lines.append(f"  assign reset = reg_reset;")
    lines.append(f"")

    # reg_input 从 formal_input 获取数据
    lines.append(f"  // 总 fuzz 输入位宽: {total_width}, 对齐到字节: {padded_width} ({n_bytes} bytes)")
    lines.append(f"  reg [{padded_width - 1}:0] reg_input;")
    lines.append(f"  always @(posedge glb_clk) begin")
    lines.append(f"    if (reset) begin")
    lines.append(f"      reg_input <= {padded_width}'b0;")
    lines.append(f"    end else begin")
    lines.append(f"      reg_input <= formal_input;")
    lines.append(f"    end")
    lines.append(f"  end")
    if insert_initial:
        lines.append(f"  initial begin")
        lines.append(f"    reg_input = '0;")
        lines.append(f"  end")
    lines.append(f"")

    # 为每个 fuzz input 声明 wire 并从 reg_input 中切片赋值
    bit_offset = 0
    lines.append(f"  // fuzz input 信号声明与赋值")
    for _, width, name in fuzz_inputs:
        if width == 1:
            lines.append(f"  wire {name};")
            lines.append(f"  assign {name} = reg_input[{bit_offset}];")
        else:
            lines.append(f"  wire [{width - 1}:0] {name};")
            lines.append(f"  assign {name} = reg_input[{bit_offset + width - 1}:{bit_offset}];")
        bit_offset += width

    # 为 output 声明 wire
    outputs = [(d, w, n) for d, w, n in all_ports if d == "output"]
    if outputs:
        lines.append(f"")
        lines.append(f"  // output 信号声明")
        for _, width, name in outputs:
            if width == 1:
                lines.append(f"  wire {name};")
            else:
                lines.append(f"  wire [{width - 1}:0] {name};")

    # 实例化原模块
    lines.append(f"")
    lines.append(f"  // 实例化原顶层模块")
    lines.append(f"  {mod_name} dut (")
    port_conns = []
    for _, _, name in all_ports:
        port_conns.append(f"    .{name}({name})")
    lines.append(",\n".join(port_conns))
    lines.append(f"  );")
    lines.append(f"")
    lines.append(f"endmodule")

    text = "\n".join(lines) + "\n"

    if output_path:
        with open(output_path, "w") as f:
            f.write(text)
        print(f"[INFO] 已生成 FormalTop 包裹模块 -> {output_path}")
        print(f"[INFO] 包裹模块: {mod_name}, fuzz 输入位宽: {total_width}, output 用 wire: {len(outputs)}")
    else:
        return text


# ============================================================
# 7. 主程序（支持任意顶层模块，中间文件存放于 $NOOP_HOME/tmp）
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Module Parser - 解析模块层次并提取指定顶层模块相关代码")
    parser.add_argument("--root-module", "-r", nargs="+", default=None,
                        help="顶层模块名（可指定多个），例如: --root-module NonBlockingDCache")
    parser.add_argument("--project-name", "-p", default=None,
                        choices=list(PROJECT_CONFIGS.keys()),
                        help=f"项目名称，自动配置默认参数 (支持: {', '.join(PROJECT_CONFIGS.keys())})")
    parser.add_argument("--svinst-parse", metavar="SV_FILE", default=None,
                        help="仅运行 svinst 解析指定 SV 文件并生成 YAML（不执行其他操作）")
    parser.add_argument("--yaml", default=None, help="YAML 文件路径 (default: SimTop.yaml)")
    parser.add_argument("--sv", default=None, help="SystemVerilog 文件路径 (default: SimTop.sv)")
    parser.add_argument("--output", "-o", default=None,
                        help="提取代码输出路径 (default: $NOOP_HOME/tmp/<root_module>_modules.sv)")
    parser.add_argument("--tree", action="store_true", help="打印完整的 SimTop 模块树")
    parser.add_argument("--module-tree", action="store_true", help="打印指定顶层模块的模块树")
    parser.add_argument("--extract", action="store_true", help="提取指定模块代码到文件")
    parser.add_argument("--list", action="store_true", help="仅列出相关模块名")
    parser.add_argument("--include-generic", action="store_true",
                        help="包含 GEN_w* 等生成的辅助模块")
    parser.add_argument("--max-depth", type=int, default=None, help="树形打印最大深度")
    parser.add_argument("--no-renumber", action="store_true",
                        help="提取时不对覆盖点重新编号")
    parser.add_argument("--initial", action="store_true",
                        help="提取时插入寄存器 initial 语句块")
    parser.add_argument("--all", action="store_true", help="执行所有操作: 打印模块树 + 列出模块 + 提取代码")
    parser.add_argument("--filter-cover", action="store_true", help="过滤 firrtl-cover.cpp 中的覆盖点")
    parser.add_argument("--cover-input", default=None,
                        help="firrtl-cover.cpp 输入路径 (default: rtl/firrtl-cover.cpp)")
    parser.add_argument("--cover-output", default=None,
                        help="过滤后的 cover 输出路径 (default: $NOOP_HOME/tmp/firrtl-cover-filtered.cpp)")
    parser.add_argument("--fuzz-wrapper", action="store_true",
                        help="生成 fuzz 包裹模块 (input 由 fuzz_get_input 驱动)")
    parser.add_argument("--fuzz-wrapper-output", default=None,
                        help="fuzz wrapper 输出路径 (default: $NOOP_HOME/tmp/SimTop.sv)")
    parser.add_argument("--formal-wrapper", action="store_true",
                        help="生成形式验证顶层包裹模块 FormalTop.sv (使用 (* gclk *) 时钟)")
    parser.add_argument("--formal-wrapper-output", default=None,
                        help="FormalTop 输出路径 (default: 脚本同目录/FormalTop.sv)")
    parser.add_argument("--fuzz-top", default=None,
                        help="要包裹的顶层模块名 (default: 第一个 --root-module)")

    args = parser.parse_args()

    # --svinst-parse: 仅运行 svinst 解析并退出
    if args.svinst_parse:
        result = run_svinst(args.svinst_parse)
        sys.exit(0 if result else 1)

    # --project-name: 完整处理流程（svinst + 提取 + wrapper + cover + GEN_*）
    project_result = None
    if args.project_name:
        print("=" * 70)
        print(f"项目初始化: {args.project_name}")
        print("=" * 70)
        project_result = setup_project(
            args.project_name,
            include_generic=args.include_generic,
            renumber=not args.no_renumber,
            insert_initial=args.initial,
        )
        if project_result is None:
            print("[ERROR] 项目初始化失败", file=sys.stderr)
            sys.exit(1)

        # setup_project 已完成所有处理，此处仅做展示操作
        root_modules = project_result["root_modules"]
        modules = project_result["modules"]
        target_mods = project_result["target_mods"]

        # 打印模块树
        if args.tree or args.module_tree:
            print()
            print("=" * 70)
            print(f"目标模块实例化树 (root: {', '.join(root_modules)})")
            print("=" * 70)
            for root in root_modules:
                print(f"\n{root}")
                print_tree(modules, root, skip_generic=not args.include_generic,
                           max_depth=args.max_depth)
            print()

        # 列出模块
        if args.list:
            print("=" * 70)
            print(f"相关模块列表 (共 {len(target_mods)} 个)")
            print("=" * 70)
            for name in sorted(target_mods):
                mod = modules.get(name)
                inst_count = len(mod.instances) if mod else 0
                print(f"  - {name} ({inst_count} sub-instances)")
            print()

        return

    # ---- 以下为非 project-name 模式：手动指定参数 ----

    # 确定 root_modules, yaml, sv 路径
    if args.root_module:
        root_modules = args.root_module
    else:
        parser.error("必须指定 --root-module 或 --project-name")

    yaml_path = args.yaml or "SimTop.yaml"
    sv_path = args.sv or "SimTop.sv"

    # 如果没有指定任何操作，默认执行 --all
    if not any([args.tree, args.module_tree, args.extract, args.list, args.all,
                args.fuzz_wrapper, args.formal_wrapper, args.filter_cover]):
        args.all = True

    # 确保 tmp 目录存在
    os.makedirs(TMP_DIR, exist_ok=True)
    print(f"[INFO] NOOP_HOME: {NOOP_HOME}")
    print(f"[INFO] 中间文件目录: {TMP_DIR}")

    # 解析 YAML
    print(f"[INFO] 解析 YAML: {yaml_path}")
    modules = parse_yaml(yaml_path)
    print(f"[INFO] 共解析 {len(modules)} 个模块定义\n")

    # 打印完整的 SimTop 模块树
    if args.tree:
        print("=" * 70)
        print("SimTop 完整模块实例化树")
        print("=" * 70)
        print("SimTop")
        print_tree(modules, "SimTop", skip_generic=not args.include_generic,
                   max_depth=args.max_depth)
        print()

    # 获取指定顶层模块的所有子模块
    target_mods = get_target_modules(modules, root_modules, include_generic=args.include_generic)

    # 打印指定模块树
    if args.module_tree or args.all:
        print("=" * 70)
        print(f"目标模块实例化树 (root: {', '.join(root_modules)})")
        print("=" * 70)
        for root in root_modules:
            print(f"\n{root}")
            print_tree(modules, root, skip_generic=not args.include_generic,
                       max_depth=args.max_depth)
        print()

    # 列出相关模块
    if args.list or args.all:
        print("=" * 70)
        print(f"相关模块列表 (共 {len(target_mods)} 个)")
        print("=" * 70)
        for name in sorted(target_mods):
            mod = modules.get(name)
            inst_count = len(mod.instances) if mod else 0
            print(f"  - {name} ({inst_count} sub-instances)")
        print()

    # 提取相关模块代码
    if args.extract or args.all:
        output_path = args.output or os.path.join(
            TMP_DIR, f"{'_'.join(root_modules)}_modules.sv"
        )
        print("=" * 70)
        print("提取相关模块代码")
        print("=" * 70)
        extract_modules(sv_path, target_mods, output_path,
                        renumber=not args.no_renumber,
                        insert_initial=args.initial)

    # 过滤 firrtl-cover.cpp 中的覆盖点
    if args.filter_cover or args.all:
        cover_input = args.cover_input or os.path.join(RTL_DIR, "firrtl-cover.cpp")
        cover_output = args.cover_output or os.path.join(TMP_DIR, "firrtl-cover-filtered.cpp")
        print("=" * 70)
        print("过滤 firrtl-cover.cpp 中的覆盖点")
        print("=" * 70)
        filter_cover_cpp(cover_input, cover_output, target_mods)

    # 生成 fuzz 包裹模块
    if args.fuzz_wrapper or args.all:
        print("=" * 70)
        print("生成 Fuzz 包裹模块")
        print("=" * 70)
        fuzz_top = args.fuzz_top or root_modules[0]
        fuzz_out = args.fuzz_wrapper_output or os.path.join(TMP_DIR, "SimTop.sv")
        generate_fuzz_wrapper(sv_path, fuzz_top, fuzz_out, insert_initial=args.initial)

    # 生成形式验证顶层包裹模块
    if args.formal_wrapper or args.all:
        print("=" * 70)
        print("生成 FormalTop 包裹模块")
        print("=" * 70)
        fuzz_top = args.fuzz_top or root_modules[0]
        formal_out = args.formal_wrapper_output or os.path.join(SCRIPT_DIR, "FormalTop.sv")
        generate_formal_wrapper(sv_path, fuzz_top, formal_out, insert_initial=args.initial)

if __name__ == "__main__":
    main()
