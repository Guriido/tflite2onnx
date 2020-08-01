import math
import logging
import tflite
from onnx import helper

from tflite2onnx import tensor
from tflite2onnx.op.operator import Operator
from tflite2onnx.op.padding import PaddingMapping, computePaddingSize
from tflite2onnx.op.activation import handleFusedActivation
from tflite2onnx.layout import Layout

logger = logging.getLogger('tflite2onnx')


class Conv(Operator):
    def __init__(self, model, graph, index):
        super().__init__(model, graph, index)

        self.auto_pad = 'SAME_UPPER'  # See ComputePaddingHeightWidth() of TFLite
        self.dilations = []
        self.group = -1
        self.kshape = []
        self.strides = []

        # ONNX: This attribute cannot be used simultaneously with `auto_pad` attribute.
        # re-initialize during self.parse(), as it needs the shape of input.
        # We prefer `auto_pad`, however ONNXRuntime doesn't support
        # `dilation` + `auto_pad`, such that we use `pads` to workaround it.
        self.pads = [0, 0, 0, 0]

        self.setInited()

    @property
    def type(self):
        if self.status.parsed and self.quantized:
            return 'QLinearConv'
        else:
            return 'Conv'

    @property
    def implicitLayout(self):
        return True

    @property
    def isDepthwise(self):
        op = self.tflite
        opcode = self.model.OperatorCodes(op.OpcodeIndex()).BuiltinCode()
        return (opcode is tflite.BuiltinOperator.DEPTHWISE_CONV_2D)

    def parse(self):
        logger.debug("Parsing %s...", self.type)
        op = self.tflite
        opcode = self.model.OperatorCodes(op.OpcodeIndex()).BuiltinCode()
        assert(opcode is tflite.BuiltinOperator.CONV_2D or tflite.BuiltinOperator.DEPTHWISE_CONV_2D)

        assert(op.InputsLength() == 3)
        assert(op.OutputsLength() == 1)

        # input
        ii = op.Inputs(0)
        ilayout = Layout('NHWC', 'NCHW')
        it = tensor.get(self.model, self.graph, ii, ilayout)
        it.parse()
        it.addConsumer(self)
        self.inputs.append(it)

        if self.quantized:
            # assert(len(it.scale)), "QLinearConv requires one scale element for input"
            assert(isinstance(it.scale, float)), "QLinearConv requires one scale for input"
            assert(isinstance(it.zero_point, int)), "QLinearConv requires one zero pint for input"
            st = tensor.createQuantScale(it)
            st.addConsumer(self)
            self.inputs.append(st)
            zpt = tensor.createQuantZeroPoint(it)
            zpt.addConsumer(self)
            self.inputs.append(zpt)

        # weight
        wi = op.Inputs(1)
        wlayout = Layout('CHWM', 'MCHW') if self.isDepthwise else Layout('OHWI', 'OIHW')
        wt = tensor.get(self.model, self.graph, wi, wlayout, True)
        wt.parse()
        wt.addConsumer(self)
        self.inputs.append(wt)

        if self.quantized:
            st = tensor.createQuantScale(wt)
            st.addConsumer(self)
            self.inputs.append(st)
            zpt = tensor.createQuantZeroPoint(wt)
            zpt.addConsumer(self)
            self.inputs.append(zpt)

        # output
        oi = op.Outputs(0)
        olayout = Layout('NHWC', 'NCHW')
        ot = tensor.get(self.model, self.graph, oi, olayout)
        ot.parse()

        if self.quantized:
            st = tensor.createQuantScale(ot)
            st.addConsumer(self)
            self.inputs.append(st)
            zpt = tensor.createQuantZeroPoint(ot)
            zpt.addConsumer(self)
            self.inputs.append(zpt)

        # bias
        bi = op.Inputs(2)
        bt = tensor.get(self.model, self.graph, bi, None, True)
        bt.parse()
        bt.addConsumer(self)
        self.inputs.append(bt)

        if self.quantized:
            bq = bt.tflite.Quantization()
            bscale = float(bq.ScaleAsNumpy()[0])
            bzp = int(bq.ZeroPointAsNumpy()[0])
            assert(bzp == 0), "Quantization semantic assertion"
            assert(math.isclose(bscale, (it.scale * wt.scale), rel_tol=1e-5))

        # options
        op_opt = op.BuiltinOptions()
        option = tflite.DepthwiseConv2DOptions() if self.isDepthwise else tflite.Conv2DOptions()
        option.Init(op_opt.Bytes, op_opt.Pos)

        self.dilations = [option.DilationHFactor(), option.DilationWFactor()]
        self.group = it.shape[3] if self.isDepthwise else 1
        self.kshape = wt.shape[1:3]
        self.strides = [option.StrideH(), option.StrideW()]
        self.auto_pad = PaddingMapping[option.Padding()]
        if self.isDepthwise:
            assert(option.DepthMultiplier() == 1)
        self.pads = computePaddingSize(option.Padding(), it.shape[1:3], self.kshape,
                                       self.strides, self.dilations)

        handleFusedActivation(self, option, ot)

        self.setParsed()

    @property
    def quantized(self):
        return tensor.isTFLiteQuantized(self.graph, self.tflite.Outputs(0))

    def transform(self):
        pass

    def convert(self):
        logger.debug("Converting %s...", self.type)

        super()._convertTensors()

        inames = [t.name for t in self.inputs]
        onames = [t.name for t in self.outputs]
        logger.debug("Making ONNX...")
        self.onnx = helper.make_node(self.type, inames, onames, kernel_shape=self.kshape,
                                     strides=self.strides, pads=self.pads,
                                     # strides=self.strides, auto_pad=self.auto_pad,
                                     dilations=self.dilations, group=self.group)

    def __str__(self):
        attrs = 'K%s, S%s, D%s, G(%d)' % (self.kshape, self.strides, self.dilations, self.group)
        return super().__str__() + attrs
