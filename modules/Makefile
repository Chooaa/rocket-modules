# bmctest/emu/Makefile — Build standalone module EMU / fuzzer binaries.
#
# Prerequisites:
#   - RTL generated:  make gen-modules  (from project root)

# ── Paths ─────────────────────────────────────────────────────────────
# Makefile 所在目录 (emu/)
EMU_DIR    := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
# rtl 目录: emu/rtl/ (包含 SimTop.sv, dcache_modules.sv, firrtl-cover.*, module_emu.cpp)
RTL_DIR    := $(EMU_DIR)/rtl

MODULE      ?= dcache
# BUILD_DIR   := $(EMU_DIR)/build
BUILD_DIR   := $(EMU_DIR)/../build
# BUILD_DIR   := $(BUILD_BASE)/$(MODULE)
BMCFUZZ ?=
ifneq ($(BMCFUZZ),)
COMPILE_DIR := $(BUILD_DIR)/fuzzer-compile
else
COMPILE_DIR := $(BUILD_DIR)/emu-compile
endif

# ── All 14 modules (excluding axi4xbar, async_queue) ─────────────────
# ALL_MODULES := broadcast xbar plic sram_ecc toaxi4 fragmenter atomic \
#                timer idpool jtag_fsm arbiter reorder_q ecc replacement
ALL_MODULES := dcache

# ── Verilator configuration ──────────────────────────────────────────
EMU_TOP := SimTop

VERILATOR_VER_CMD = verilator --version 2>/dev/null | cut -f2 -d' ' | tr -d '.'
VERILATOR_5_000 := $(shell expr `$(VERILATOR_VER_CMD)` \>= 5000 2>/dev/null)

VFLAGS  = --cc --exe
VFLAGS += --top-module $(EMU_TOP)
VFLAGS += +define+VERILATOR=1
VFLAGS += +define+RANDOMIZE_REG_INIT
VFLAGS += +define+PRINTF_COND=1
VFLAGS += +define+DIFFTEST
VFLAGS += -Wno-STMTDLY -Wno-WIDTH
VFLAGS += --assert --x-assign unique
VFLAGS += --output-split 30000
VFLAGS += --output-split-cfuncs 30000
VFLAGS += --max-num-width 150000
VFLAGS += -O3

ifeq ($(VERILATOR_5_000),1)
VFLAGS += --no-timing +define+VERILATOR_5
else
VFLAGS += +define+VERILATOR_LEGACY
endif

# Waveform trace support (VCD or FST)
EMU_TRACE ?=
EMU_TRACE_FORMAT ?= fst
ifneq ($(EMU_TRACE),)
ifeq ($(EMU_TRACE_FORMAT),fst)
VFLAGS += --trace-fst
else
VFLAGS += --trace
endif
VFLAGS += --trace-max-array 512
VFLAGS += --trace-max-width 200000
endif

# ── Source files ──────────────────────────────────────────────────────
# RTL 来自 rtl 目录 (SimTop.sv 为顶层)
RTL_SV     = $(wildcard $(RTL_DIR)/*.sv)
RTL_V      = $(wildcard $(RTL_DIR)/GEN_w*.v)
# RTL_EXTRA  = $(wildcard $(RTL_DIR)/plusarg_reader.v)

# C++ sources (module_emu.cpp 和 firrtl-cover 均在 rtl 目录)
EMU_CPP    = $(EMU_DIR)/module_emu.cpp
COVER_CPP  = $(wildcard $(RTL_DIR)/firrtl-cover.cpp)
COVER_H    = $(RTL_DIR)/firrtl-cover.h

ALL_VSRC = $(RTL_SV) $(RTL_V) $(RTL_EXTRA)
ALL_CSRC = $(EMU_CPP) $(COVER_CPP)

# ── C++ flags ─────────────────────────────────────────────────────────
EMU_CXXFLAGS  = -DVERILATOR -std=c++17
EMU_CXXFLAGS += -I$(dir $(COVER_H))

FIRRTL_COVER_EXISTS := $(wildcard $(COVER_H))
ifneq ($(FIRRTL_COVER_EXISTS),)
EMU_CXXFLAGS += -DFIRRTL_COVER
endif

EMU_LDFLAGS =

# Trace support
ifneq ($(EMU_TRACE),)
EMU_CXXFLAGS += -DVM_TRACE=1
ifeq ($(EMU_TRACE_FORMAT),fst)
EMU_CXXFLAGS += -DVM_TRACE_FST=1
endif
endif

# ── Fuzzer mode ───────────────────────────────────────────────────────
ifneq ($(XFUZZ),)
XFUZZ_HOME ?= $(abspath $(EMU_DIR)/..)/ccover
FUZZER_LIB    = $(XFUZZ_HOME)/target/release/libfuzzer.a

EMU_CXXFLAGS += -DFUZZER_LIB
EMU_LDFLAGS  += $(abspath $(FUZZER_LIB)) -lrt -lpthread

TARGET = $(BUILD_DIR)/fuzzer
else
TARGET = $(BUILD_DIR)/emu
endif

# EMU_CXXFLAGS += -g

# ── Verilator Makefile generation ─────────────────────────────────────
VMAKE = $(COMPILE_DIR)/V$(EMU_TOP).mk

$(VMAKE): $(ALL_VSRC)
	@mkdir -p $(COMPILE_DIR)
	verilator $(VFLAGS) \
		-Mdir $(COMPILE_DIR) \
		-CFLAGS "$(EMU_CXXFLAGS)" \
		$(if $(EMU_LDFLAGS),-LDFLAGS "$(EMU_LDFLAGS)") \
		-o $(abspath $(TARGET)) \
		$(ALL_VSRC) $(ALL_CSRC)
ifneq ($(VERILATOR_5_000),1)
	@sed -i 's/private/public/g' $(COMPILE_DIR)/VSimTop.h 2>/dev/null || true
endif

# ── Build target ──────────────────────────────────────────────────────
$(TARGET): $(VMAKE) $(ALL_CSRC)
	@echo "[module-emu] Building $(MODULE) → $(TARGET)"
	$(MAKE) -s VM_PARALLEL_BUILDS=1 OPT_FAST="-O3" OPT_SLOW="-O0" \
		-C $(COMPILE_DIR) -f V$(EMU_TOP).mk

# ── User-facing targets ──────────────────────────────────────────────
.PHONY: emu clean help

all: emu

emu: $(TARGET)
	@echo "[module-emu] Done: $(TARGET)"

clean:
	rm -rf $(BUILD_DIR)

help:
	@echo "Module EMU/Fuzzer Build System"
	@echo ""
	@echo "Targets:"
	@echo "  emu          Build standalone EMU binary (MODULE=<name>)"
	@echo "  fuzzer       Build fuzzer binary (MODULE=<name> BMCFUZZ=1)"
	@echo "  emu-all      Build EMU for all 14 modules"
	@echo "  fuzzer-all   Build fuzzer for all 14 modules"
	@echo "  clean        Remove build for current MODULE"
	@echo "  clean-all    Remove all build artifacts"
	@echo ""
	@echo "Variables:"
	@echo "  MODULE       Target module name (default: timer)"
	@echo "  BMCFUZZ      Enable fuzzer mode (set to 1)"
	@echo "  EMU_TRACE    Enable VCD trace (set to 1)"
	@echo "  BMCFUZZ_HOME Path to ccover (default: ../../ccover)"
	@echo ""
	@echo "Available modules: $(ALL_MODULES)"
