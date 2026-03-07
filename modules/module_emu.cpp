/*
 * module_emu.cpp — Shared C++ emulator driver for standalone module fuzzing.
 *
 * Provides:
 *   - fuzz_get_byte() DPI-C: feeds fuzzer input bytes to the SV wrapper
 *   - Two entry modes: FUZZER_LIB (linked with libfuzzer.a) or standalone
 *   - Verilator simulation loop with reset, clock toggle, io_success check
 *   - FIRRTL coverage integration (firrtl-cover.h/cpp)
 *   - Optional VCD trace (VM_TRACE)
 */

#include "verilated.h"

#if VM_TRACE
#include <memory>
#if VM_TRACE_FST
#include "verilated_fst_c.h"
typedef VerilatedFstC TraceFile;
#else
#include "verilated_vcd_c.h"
typedef VerilatedVcdC TraceFile;
#endif
#endif

#ifdef FIRRTL_COVER
#include "firrtl-cover.h"
#endif

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <getopt.h>
#include <signal.h>

#include "VSimTop.h"

// ── Fuzz input buffer ────────────────────────────────────────────────
static const uint8_t *fuzz_buf = nullptr;
static size_t fuzz_buf_len = 0;
static size_t fuzz_buf_pos = 0;

extern "C" unsigned char fuzz_get_byte() {
    if (fuzz_buf && fuzz_buf_pos < fuzz_buf_len) {
        // printf("FUZZ_GET_BYTE: pos=%zu, byte=0x%02x\n", fuzz_buf_pos, fuzz_buf[fuzz_buf_pos]);
        return fuzz_buf[fuzz_buf_pos++];
    }
    return 0;
}

// ── Simulation state ─────────────────────────────────────────────────
static uint64_t trace_count = 0;
static volatile bool sig_exit = false;
static bool _sim_verbose = false;
static char cover_feedback_name[1024] = {0};

// global variable for record if new control cover points are covered (used by firrtl-cover.cpp v_cover_control)
bool new_points_covered = false;

double sc_time_stamp() { return trace_count; }

extern "C" int vpi_get_vlog_info(void *arg) { return 0; }

static void handle_sigterm(int) { sig_exit = true; }

// ── Coverage helpers ─────────────────────────────────────────────────
#ifdef FIRRTL_COVER
static const int n_cover_types =
    sizeof(firrtl_cover) / sizeof(FIRRTLCoverPointParam);

static uint32_t get_cover_total() {
    uint32_t total = 0;
    if (strlen(cover_feedback_name) > 0 && cover_feedback_name[0] != '\0') {
        for (int i = 0; i < n_cover_types; i++) {
            if (strcmp(cover_feedback_name, firrtl_cover[i].cover.name) == 0) {
                total += firrtl_cover[i].cover.total;
            }
        }
    } else {
        for (int i = 0; i < n_cover_types; i++) {
            total += firrtl_cover[i].cover.total;
        }
    }
    return total;
}

static uint32_t get_cover_hit() {
    uint32_t hit = 0;
    if (strlen(cover_feedback_name) > 0 && cover_feedback_name[0] != '\0') {
        for (int i = 0; i < n_cover_types; i++) {
            if (strcmp(cover_feedback_name, firrtl_cover[i].cover.name) == 0) {
                for (uint64_t j = 0; j < firrtl_cover[i].cover.total; j++) {
                    if (firrtl_cover[i].cover.points[j]) hit++;
                }
            }
        }
    } else {
        for (int i = 0; i < n_cover_types; i++) {
            for (uint64_t j = 0; j < firrtl_cover[i].cover.total; j++) {
                if (firrtl_cover[i].cover.points[j]) hit++;
            }
        }
    }
    return hit;
}

static void reset_cover() {
    for (int i = 0; i < n_cover_types; i++) {
        memset(firrtl_cover[i].cover.points, 0, firrtl_cover[i].cover.total);
    }
}

static void display_cover() {
    for (int i = 0; i < n_cover_types; i++) {
        uint32_t total = firrtl_cover[i].cover.total;
        uint32_t hit = 0;
        for (uint64_t j = 0; j < total; j++) {
            if (firrtl_cover[i].cover.points[j]) hit++;
        }
        fprintf(stderr, "COVERAGE: %s, %u / %u (%.1f%%)\n",
                firrtl_cover[i].cover.name, hit, total,
                total ? 100.0 * hit / total : 0.0);
    }
}
#endif // FIRRTL_COVER

// ── Accumulative control coverage for fuzzer feedback ────────────────────────
#ifdef FIRRTL_COVER
uint64_t acc_cover_size = 0;
uint64_t acc_covered_num = 0;
uint8_t *acc_cover;

static void init_acc_cover() {
    for (int i = 0; i < n_cover_types; i++) {
        if (strcmp(firrtl_cover[i].cover.name, "control") == 0) {
            acc_cover = new uint8_t[firrtl_cover[i].cover.total]();
            acc_cover_size = firrtl_cover[i].cover.total;
            acc_covered_num = 0;
            memset(acc_cover, 0, acc_cover_size);
            break;
        }
    }
}

static void accumulate_cover() {
    for (int i = 0; i < n_cover_types; i++) {
        if (strcmp(firrtl_cover[i].cover.name, "control") == 0) {
            for (uint64_t j = 0; j < firrtl_cover[i].cover.total; j++) {
                if (firrtl_cover[i].cover.points[j]) {
                    acc_cover[j] = 1;
                }
            }
        }
    }
}

static void free_acc_cover() {
    if (acc_cover) {
        delete[] acc_cover;
        acc_cover = nullptr;
    }
}

static void display_acc_cover() {
    // uint64_t total = 0;
    // for (uint64_t j = 0; j < acc_cover_size; j++) {
    //     if (acc_cover[j]) total++;
    // }
    // fprintf(stderr, "ACC_COVER: %lu / %lu (%.1f%%)\n", total, acc_cover_size,
    //         acc_cover_size ? 100.0 * total / acc_cover_size : 0.0);
    printf("ACC_COVER: %lu / %lu (%.1f%%)\n", acc_covered_num, acc_cover_size,
            acc_cover_size ? 100.0 * acc_covered_num / acc_cover_size : 0.0);
    // printf("ACC_COVER: %lu / %lu (%.1f%%)\n", total, acc_cover_size,
    //         acc_cover_size ? 100.0 * total / acc_cover_size : 0.0);
}

#endif // FIRRTL_COVER

// ── Exported interface for fuzzer library ────────────────────────────
#ifdef FUZZER_LIB
extern "C" uint32_t get_cover_number() {
#ifdef FIRRTL_COVER
    return get_cover_total();
#else
    return 0;
#endif
}

extern "C" void update_stats(uint8_t *bytes) {
#ifdef FIRRTL_COVER
    if (strlen(cover_feedback_name) > 0 && cover_feedback_name[0] != '\0') {
        for (int i = 0; i < n_cover_types; i++) {
            if (strcmp(cover_feedback_name, firrtl_cover[i].cover.name) == 0) {
                memcpy(bytes, firrtl_cover[i].cover.points, firrtl_cover[i].cover.total);
                bytes += firrtl_cover[i].cover.total;
            }
        }
    } else {
        for (int i = 0; i < n_cover_types; i++) {
            memcpy(bytes, firrtl_cover[i].cover.points, firrtl_cover[i].cover.total);
            bytes += firrtl_cover[i].cover.total;
        }
    }
#endif
}

extern "C" void set_cover_feedback(const char *name) {
    // In standalone mode, feedback cover is always the first type.
    strncpy(cover_feedback_name, name, sizeof(cover_feedback_name));
    printf("Cover feedback name: %s\n", cover_feedback_name);
}

extern "C" void enable_sim_verbose()  { _sim_verbose = true;  }
extern "C" void disable_sim_verbose() { _sim_verbose = false; }
#endif // FUZZER_LIB

uint64_t fuzz_id = 0;
inline char *snapshot_wavefile_name(uint64_t cycle) {
    static char buf[1024];
    const char *noop_home = getenv("NOOP_HOME");
    assert(noop_home && "NOOP_HOME environment variable must be set for snapshot waveforms");
    // snprintf(buf, sizeof(buf), "%s/tmp/fuzz_run/%lu/snapshot-%lu.fst", noop_home, fuzz_id, cycle);
    snprintf(buf, sizeof(buf), "%s/tmp/fuzz_run/snapshot-%lu-%lu.fst", noop_home, fuzz_id, cycle);
    return buf;
}
inline char *control_cover_points_file_name(uint64_t cycle) {
    static char buf[1024];
    const char *noop_home = getenv("NOOP_HOME");
    assert(noop_home && "NOOP_HOME environment variable must be set for control cover points file");
    snprintf(buf, sizeof(buf), "%s/tmp/fuzz_run/control_cover_points-%lu-%lu.csv", noop_home, fuzz_id, cycle);
    return buf;
}

bool run_snapshot = false;
bool dump_snapshot = false;
uint64_t snapshot_cycle = 0;

// ── Simulation core ──────────────────────────────────────────────────

static int run_sim(int argc, const char **argv,
                   const uint8_t *input, size_t input_len,
                   uint64_t max_cycles, const char *vcd_path) {
    fuzz_buf = input;
    fuzz_buf_len = input_len;
    fuzz_buf_pos = 0;
    trace_count = 0;
    sig_exit = false;

    // Per-run context: avoids stale traceBaseModelCb accumulation in the
    // global default context across repeated sim_main calls.
    VerilatedContext *contextp = new VerilatedContext;
    contextp->commandArgs(argc, argv);
    contextp->randReset(2);
    contextp->gotError(false);
    contextp->gotFinish(false);
    contextp->fatalOnError(false);

    VSimTop *top = new VSimTop{contextp};

#if VM_TRACE
    contextp->traceEverOn(true);
    TraceFile *tfp = nullptr;
    TraceFile *snapshot_tfp = nullptr;

    if (vcd_path) {
        tfp = new TraceFile;
        top->trace(tfp, 99);
        tfp->open(vcd_path);
    }
    if (dump_snapshot) {
        // Create ONE persistent snapshot trace; register it ONCE with top.
        // Reuse via open/close per snapshot to avoid use-after-free from
        // repeated top->trace() + delete cycles.
        snapshot_tfp = new TraceFile;
        top->trace(snapshot_tfp, 99);
        printf("Snapshot dumping enabled, will save snapshot on new cover points\n");
    }
#endif

    new_points_covered = false;
    bool check_snapshot = false;

    int ret = 0;
    // bool done_reset = false;

    const int reset_cycles = 10;

    while (trace_count < max_cycles && !sig_exit) {
        if (contextp->gotError() || contextp->gotFinish())
            break;
        // if (done_reset)
        //     break;
        
        // printf("Cycle %lu: reset=%d\n", trace_count, (trace_count < (uint64_t)reset_cycles) ? 1 : 0);

        check_snapshot = new_points_covered;

        top->clock = 0;
        if (!run_snapshot) {
            top->reset = (trace_count < (uint64_t)reset_cycles) ? 1 : 0;
        } else {
            top->reset = 0;
        }
        // done_reset = !top->reset;
        top->eval();
        if (contextp->gotError()) break;

#if VM_TRACE
        if (tfp) tfp->dump(static_cast<vluint64_t>(trace_count * 2));
        if (dump_snapshot && check_snapshot && snapshot_tfp) {
            // printf("Control cover points covered at cycle %lu\n", trace_count);
            snapshot_tfp->open(snapshot_wavefile_name(trace_count));
            snapshot_tfp->dump(static_cast<vluint64_t>(trace_count * 2));
        }
#endif

        top->clock = 1;
        top->eval();
        if (contextp->gotError()) break;

#if VM_TRACE
        if (tfp) tfp->dump(static_cast<vluint64_t>(trace_count * 2 + 1));
        if (dump_snapshot && check_snapshot && snapshot_tfp) {
            snapshot_tfp->dump(static_cast<vluint64_t>(trace_count * 2 + 1));
            snapshot_tfp->close();
            printf("Snapshot FST saved: %s\n", snapshot_wavefile_name(trace_count));
            new_points_covered = false;
            // write control cover points to file
            display_acc_cover();
            FILE *fp = fopen(control_cover_points_file_name(trace_count), "w");
            if (fp) {
                fprintf(fp, "Index,Covered\n");
                for (uint64_t j = 0; j < acc_cover_size; j++) {
                    fprintf(fp, "%lu,%d\n", j, acc_cover[j] ? 1 : 0);
                }
                fclose(fp);
            }
        }
#endif
        // printf("Cycle %lu: sig_exit=%s\n", trace_count, sig_exit ? "true" : "false");
        trace_count++;
    }

    if (contextp->gotError()) {
        ret = 1;
    } else if (trace_count >= max_cycles) {
        ret = 2;
    }

    printf("Simulation finished after %lu cycles, result: %s\n", trace_count,
           (ret == 0) ? "PASS" : (ret == 1) ? "FAIL" : "TIMEOUT");

#if VM_TRACE
    if (snapshot_tfp) {
        snapshot_tfp->close();
        delete snapshot_tfp;
    }
    if (tfp) {
        tfp->close();
        delete tfp;
    }
#endif

    delete top;
    delete contextp;
    return ret;
}

// ── Entry points ─────────────────────────────────────────────────────

#ifdef FUZZER_LIB

extern "C" int sim_main(int argc, const char **argv) {
    uint64_t max_cycles = 10000;
    const char *wave_path = nullptr;
    for (int i = 1; i < argc; i++) {
        if (strncmp(argv[i], "--max-cycles=", 13) == 0) {
            max_cycles = strtoull(argv[i] + 13, nullptr, 10);
        } else if (strcmp(argv[i], "-m") == 0 && i + 1 < argc) {
            max_cycles = strtoull(argv[++i], nullptr, 10);
        }
        else if (strcmp(argv[i], "--dump-wave") == 0) {
            wave_path = argv[++i];
        }
        else if (strcmp(argv[i], "--fuzz-id") == 0 && i + 1 < argc) {
            fuzz_id = strtoull(argv[++i], nullptr, 10);
        }
        else if (strcmp(argv[i], "--run-snapshot") == 0) {
            run_snapshot = true;
        }
        else if (strcmp(argv[i], "--dump-snapshot") == 0) {
            dump_snapshot = true;
        }
    }

    // printf("Max cycles: %lu\n", max_cycles);
    // printf("Fuzz ID: %lu\n", fuzz_id);

    const uint8_t *input = nullptr;
    size_t input_len = 0;
    bool input_is_borrowed = false;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-i") == 0 && i + 1 < argc) {
            const char *path = argv[++i];
            // wim@0xADDR+0xLEN — workload-in-memory from Rust fuzzer
            if (strncmp(path, "wim@", 4) == 0) {
                char *endp;
                uintptr_t addr = strtoull(path + 4, &endp, 16);
                if (*endp == '+') {
                    input_len = (size_t)strtoull(endp + 1, nullptr, 16);
                    input = reinterpret_cast<const uint8_t *>(addr);
                    input_is_borrowed = true;
                }
            } else {
                FILE *fp = fopen(path, "rb");
                if (fp) {
                    fseek(fp, 0, SEEK_END);
                    input_len = ftell(fp);
                    printf("Read input file: %s, size: %zu bytes\n", path, input_len);
                    fseek(fp, 0, SEEK_SET);
                    uint8_t *buf = new uint8_t[input_len];
                    if (fread(buf, 1, input_len, fp) == input_len)
                        input = buf;
                    fclose(fp);
                }
            }
        }
    }

#ifdef FIRRTL_COVER
    init_acc_cover();
    // reset_cover();
#endif

    int ret = run_sim(argc, argv, input, input_len, max_cycles, wave_path);

#ifdef FIRRTL_COVER
    // display_acc_cover();
    free_acc_cover();
    // accumulate_cover();
    // display_cover();
#endif

    if (!input_is_borrowed)
        delete[] input;
    return ret;
}

#else // standalone mode

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage: %s [options]\n"
        "  -i FILE        Input binary file for fuzz bytes\n"
        "  -m CYCLES      Max simulation cycles (default: 100000)\n"
        "  -v FILE        VCD output file\n"
        "  -s SEED        Random seed\n"
        "  -h             Show this help\n",
        prog);
}

int main(int argc, char **argv) {
    uint64_t max_cycles = 100000;
    unsigned seed = 0;
    const char *input_path = nullptr;
    const char *vcd_path = nullptr;
    bool has_seed = false;

    int opt;
    while ((opt = getopt(argc, argv, "i:m:v:s:r:d:h")) != -1) {
        switch (opt) {
        case 'i': input_path = optarg; break;
        case 'm': max_cycles = strtoull(optarg, nullptr, 10); break;
        case 'v': vcd_path = optarg; break;
        case 's': seed = atoi(optarg); has_seed = true; break;
        case 'r': run_snapshot = true; break;
        case 'd': dump_snapshot = true; break;
        case 'h':
        default:  usage(argv[0]); return (opt == 'h') ? 0 : 1;
        }
    }

    if (has_seed) {
        srand(seed);
        srand48(seed);
    }

    uint8_t *input = nullptr;
    size_t input_len = 0;
    if (input_path) {
        FILE *fp = fopen(input_path, "rb");
        if (!fp) {
            fprintf(stderr, "ERROR: cannot open %s\n", input_path);
            return 1;
        }
        fseek(fp, 0, SEEK_END);
        input_len = ftell(fp);
        fseek(fp, 0, SEEK_SET);
        input = new uint8_t[input_len];
        if (fread(input, 1, input_len, fp) != input_len) {
            fprintf(stderr, "ERROR: failed to read %s\n", input_path);
            fclose(fp);
            delete[] input;
            return 1;
        }
        fclose(fp);
    }

    signal(SIGTERM, handle_sigterm);

#ifdef FIRRTL_COVER
    init_acc_cover();
    reset_cover();
#endif

    int ret = run_sim(argc, (const char **)argv, input, input_len, max_cycles, vcd_path);

#ifdef FIRRTL_COVER
    // accumulate_cover();
    display_cover();
    uint32_t total = get_cover_total();
    uint32_t hit = get_acc_cover_hit();
    fprintf(stderr, "COVERAGE TOTAL: %u / %u (%.1f%%)\n",
            hit, total, total ? 100.0 * hit / total : 0.0);
    free_acc_cover();
#endif

    if (ret == 2) {
        fprintf(stderr, "*** TIMEOUT *** after %lu cycles\n", trace_count);
    } else if (ret == 0) {
        fprintf(stderr, "*** PASSED *** after %lu cycles\n", trace_count);
    }

    delete[] input;
    return ret;
}

#endif // FUZZER_LIB
