import argparse

from m5.params import NULL  # type: ignore
from gem5.components.boards.simple_board import SimpleBoard  # type: ignore
from gem5.components.processors.base_cpu_core import BaseCPUCore  # type: ignore
from gem5.components.processors.base_cpu_processor import BaseCPUProcessor  # type: ignore
from gem5.components.memory.simple import SingleChannelSimpleMemory  # type: ignore
from gem5.components.cachehierarchies.classic.private_l1_cache_hierarchy import (  # type: ignore
    PrivateL1CacheHierarchy,
)
from gem5.isas import ISA  # type: ignore
from gem5.simulate.simulator import Simulator  # type: ignore
from gem5.resources.resource import BinaryResource  # type: ignore

from m5.objects import (  # type: ignore
    LocalBP,
    LRURP,
    MinorFUPool,
    MinorDefaultFloatSimdFU,
    MinorDefaultPredFU,
    MinorDefaultMiscFU,
    MinorFU,
    MinorFUTiming,
    MinorOpClassSet,
    MinorOpClass,
    ReturnAddrStack,
    RiscvMinorCPU,
    SimpleBTB,
)

# Reference Core used in the CARLA 2026 paper, "MinorFlow: A gem5 Pipeline
# Visualizer for Teaching Computer Architecture". A single-issue in-order 64-bit
# RISC-V MinorCPU at 100 MHz with a 16 KiB L1I, a 32 KiB L1D and a local branch
# predictor, matching Table 1 of the paper.

L1I_SIZE = "16KiB"
L1D_SIZE = "32KiB"
CLK_FREQ = "100MHz"


def minorMakeOpClassSet(op_classes):
    def boxOpClass(op_class):
        return MinorOpClass(opClass=op_class)
    return MinorOpClassSet(opClasses=[boxOpClass(o) for o in op_classes])


class RISCVFUPool(MinorFUPool):
    def __init__(self):
        super().__init__()

        int_alu = MinorFU()
        int_alu.opClasses = minorMakeOpClassSet(['IntAlu'])
        int_alu.opLat = 1
        int_alu.issueLat = 1

        int_mul = MinorFU()
        int_mul.opClasses = minorMakeOpClassSet(['IntMult'])
        int_mul.opLat = 4
        int_mul.issueLat = 1

        int_div = MinorFU()
        int_div.opClasses = minorMakeOpClassSet(['IntDiv'])
        int_div.opLat = 20
        int_div.issueLat = 20

        fp_fast_A = MinorFU(
            opClasses=minorMakeOpClassSet(['FloatAdd', 'FloatCvt']),
            opLat=3, issueLat=1
        )

        fp_fast_B = MinorFU(
            opClasses=minorMakeOpClassSet(['FloatMult', 'FloatMultAcc']),
            opLat=4, issueLat=1
        )

        fp_sqrt = MinorFU(
            opClasses=minorMakeOpClassSet(['FloatSqrt']),
            opLat=30, issueLat=25
        )

        fp_div = MinorFU(
            opClasses=minorMakeOpClassSet(['FloatDiv']),
            opLat=25, issueLat=22
        )

        fp_cmp = MinorFU(
            opClasses=minorMakeOpClassSet(['FloatCmp']),
            opLat=2, issueLat=1
        )

        mem_fu = MinorFU()
        mem_fu.opClasses = minorMakeOpClassSet(['MemRead', 'MemWrite'])
        mem_fu.opLat = 2
        mem_fu.issueLat = 1

        simd_int_fast = MinorDefaultFloatSimdFU()
        simd_int_fast.opClasses = minorMakeOpClassSet([
            'SimdAdd', 'SimdAlu', 'SimdCmp', 'SimdShift',
            'SimdMisc', 'SimdExt', 'SimdConfig', 'FloatMisc'
        ])
        simd_int_fast.timings = [MinorFUTiming(
            description='SimdIntFast', srcRegsRelativeLats=[2])]
        simd_int_fast.opLat = 2
        simd_int_fast.issueLat = 1

        simd_complex = MinorDefaultFloatSimdFU()
        simd_complex.opClasses = minorMakeOpClassSet([
            'SimdAddAcc', 'SimdCvt', 'SimdMult', 'SimdMultAcc',
            'SimdFloatAdd', 'SimdFloatAlu', 'SimdFloatCmp', 'SimdFloatCvt',
            'SimdFloatMisc', 'SimdFloatMult', 'SimdFloatMultAcc', 'SimdFloatExt',
            'SimdReduceAdd', 'SimdReduceAlu', 'SimdReduceCmp',
            'SimdFloatReduceAdd', 'SimdFloatReduceCmp',
            'SimdAes', 'SimdAesMix', 'SimdSha1Hash', 'SimdSha1Hash2',
            'SimdSha256Hash', 'SimdSha256Hash2', 'SimdShaSigma2', 'SimdShaSigma3'
        ])
        simd_complex.timings = [MinorFUTiming(
            description='SimdComplex', srcRegsRelativeLats=[2])]
        simd_complex.opLat = 4
        simd_complex.issueLat = 1

        simd_matrix = MinorDefaultFloatSimdFU()
        simd_matrix.opClasses = minorMakeOpClassSet([
            'Matrix', 'MatrixMov', 'MatrixOP',
            'SimdMatMultAcc', 'SimdFloatMatMultAcc'
        ])
        simd_matrix.timings = [MinorFUTiming(
            description='SimdMatrix', srcRegsRelativeLats=[2])]
        simd_matrix.opLat = 6
        simd_matrix.issueLat = 2

        simd_div_sqrt = MinorDefaultFloatSimdFU()
        simd_div_sqrt.opClasses = minorMakeOpClassSet([
            'SimdDiv', 'SimdSqrt', 'SimdFloatDiv', 'SimdFloatSqrt'
        ])
        simd_div_sqrt.timings = [MinorFUTiming(
            description='SimdDivSqrt', srcRegsRelativeLats=[2])]
        simd_div_sqrt.opLat = 15
        simd_div_sqrt.issueLat = 12

        pred = MinorDefaultPredFU()
        pred.opClasses = minorMakeOpClassSet(['SimdPredAlu'])
        pred.timings = [MinorFUTiming(
            description='Pred', srcRegsRelativeLats=[2])]
        pred.opLat = 1
        pred.issueLat = 1

        vec_mem_fast = MinorFU()
        vec_mem_fast.opClasses = minorMakeOpClassSet([
            'FloatMemRead', 'FloatMemWrite',
            'SimdUnitStrideLoad', 'SimdUnitStrideStore',
            'SimdUnitStrideMaskLoad', 'SimdUnitStrideMaskStore',
            'SimdUnitStrideFaultOnlyFirstLoad',
            'SimdWholeRegisterLoad', 'SimdWholeRegisterStore'
        ])
        vec_mem_fast.timings = [MinorFUTiming(
            description='VecMemFast', srcRegsRelativeLats=[1], extraAssumedLat=2)]
        vec_mem_fast.opLat = 2
        vec_mem_fast.issueLat = 1

        vec_mem_slow = MinorFU()
        vec_mem_slow.opClasses = minorMakeOpClassSet([
            'SimdStridedLoad', 'SimdStridedStore',
            'SimdIndexedLoad', 'SimdIndexedStore'
        ])
        vec_mem_slow.timings = [MinorFUTiming(
            description='VecMemSlow', srcRegsRelativeLats=[1], extraAssumedLat=2)]
        vec_mem_slow.opLat = 10
        vec_mem_slow.issueLat = 4

        misc = MinorDefaultMiscFU()
        misc.opClasses = minorMakeOpClassSet(['InstPrefetch'])
        misc.opLat = 1
        misc.issueLat = 1

        self.funcUnits = [
            int_alu, int_mul, int_div, fp_fast_A, fp_fast_B,
            fp_sqrt, fp_div, fp_cmp, mem_fu, simd_int_fast,
            simd_complex, simd_matrix, simd_div_sqrt, pred,
            vec_mem_fast, vec_mem_slow, misc,
        ]


class CPU(RiscvMinorCPU):
    def __init__(self):
        super().__init__()

        self.executeFuncUnits = RISCVFUPool()

        self.fetch1FetchLimit = 1
        self.fetch1LineSnapWidth = 4
        self.fetch1LineWidth = 4
        self.fetch1ToFetch2ForwardDelay = 1
        self.fetch1ToFetch2BackwardDelay = 1
        self.fetch2InputBufferSize = 3
        self.fetch2ToDecodeForwardDelay = 1
        self.fetch2CycleInput = False
        self.decodeInputBufferSize = 4
        self.decodeToExecuteForwardDelay = 1
        self.decodeInputWidth = 1
        self.decodeCycleInput = False
        self.executeInputWidth = 1
        self.executeCycleInput = False
        self.executeIssueLimit = 1
        self.executeMemoryIssueLimit = 1
        self.executeCommitLimit = 2
        self.executeMemoryCommitLimit = 1
        self.executeInputBufferSize = 8
        self.executeMemoryWidth = 8
        self.executeMaxAccessesInMemory = 8
        self.executeLSQMaxStoreBufferStoresPerCycle = 1
        self.executeLSQRequestsQueueSize = 4
        self.executeLSQTransfersQueueSize = 8
        self.executeLSQStoreBufferSize = 16
        self.executeBranchDelay = 1
        self.executeSetTraceTimeOnCommit = True
        self.executeSetTraceTimeOnIssue = False
        self.executeAllowEarlyMemoryIssue = True
        self.enableIdling = False

        self.branchPred = LocalBP(
            localPredictorSize=1024,
            localCtrBits=2,
            instShiftAmt=1,
        )
        self.branchPred.btb = SimpleBTB(
            numEntries=256,
            tagBits=20,
            associativity=4,
            instShiftAmt=1,
            btbReplPolicy=LRURP(),
        )
        self.branchPred.ras = ReturnAddrStack(
            numEntries=16,
        )


class Processor(BaseCPUProcessor):
    def __init__(self):
        cpu = CPU()
        core = BaseCPUCore(core=cpu, isa=ISA.RISCV)
        super().__init__(cores=[core])


class CacheHierarchy(PrivateL1CacheHierarchy):
    def __init__(self, l1d_size, l1i_size):
        super().__init__(l1d_size=l1d_size, l1i_size=l1i_size)

    def incorporate_cache(self, board):
        super().incorporate_cache(board)

        for i, core in enumerate(board.get_processor().get_cores()):
            self.l1icaches[i].assoc = 4
            self.l1icaches[i].tag_latency = 1
            self.l1icaches[i].data_latency = 1
            self.l1icaches[i].response_latency = 1
            self.l1icaches[i].mshrs = 4
            self.l1icaches[i].tgts_per_mshr = 16
            self.l1icaches[i].is_read_only = True
            self.l1icaches[i].sequential_access = False
            self.l1icaches[i].writeback_clean = False

            self.l1dcaches[i].assoc = 8
            self.l1dcaches[i].tag_latency = 1
            self.l1dcaches[i].data_latency = 1
            self.l1dcaches[i].response_latency = 1
            self.l1dcaches[i].mshrs = 8
            self.l1dcaches[i].tgts_per_mshr = 16
            self.l1dcaches[i].write_buffers = 8
            self.l1dcaches[i].is_read_only = False
            self.l1dcaches[i].sequential_access = False
            self.l1dcaches[i].writeback_clean = False
            self.l1dcaches[i].prefetcher = NULL


parser = argparse.ArgumentParser(description="RISC-V Reference Core on gem5")
parser.add_argument("binary", type=str,
                    help="Path to the compiled RISC-V ELF binary")
args = parser.parse_args()

binary = BinaryResource(args.binary)

processor = Processor()

cache_hierarchy = CacheHierarchy(
    l1d_size=L1D_SIZE,
    l1i_size=L1I_SIZE,
)

memory = SingleChannelSimpleMemory(
    latency="60ns",
    latency_var="0ns",
    bandwidth="1.6GiB/s",
    size="1GiB",
)

board = SimpleBoard(
    clk_freq=CLK_FREQ,
    processor=processor,
    memory=memory,
    cache_hierarchy=cache_hierarchy,
)

board.cache_line_size = 64
board.set_se_binary_workload(binary)

simulator = Simulator(board=board)
print("Starting simulation")
simulator.run()
