package org.mif.inference;

import ai.djl.Device;
import ai.djl.Model;
import ai.djl.engine.Engine;
import ai.djl.util.cuda.CudaUtils;
import ai.djl.inference.Predictor;
import ai.djl.ndarray.NDList;
import ai.djl.ndarray.types.DataType;
import ai.djl.ndarray.types.Shape;
import ai.djl.translate.Batchifier;
import ai.djl.translate.Translator;
import ai.djl.translate.TranslatorContext;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Path;

/** One loaded frozen model and predictor, with temporary tensors per patch.
 * The caller supplies raw, already-masked C,Y,X samples. The exported graph
 * owns the fixed panel normalization and marker embeddings. The direct host
 * buffer remains alive until synchronous prediction has returned; GPU engines
 * may copy it to their selected device. Returned features are owned host arrays.
 */
public final class FrozenEncoder implements AutoCloseable {
    private final Model model;
    private final Predictor<ByteBuffer, float[]> predictor;
    private final ByteBuffer input;
    private final int elements;
    private boolean closed;
    private long calls;

    public FrozenEncoder(Path weights, int channels, int patchSize, String device) throws Exception {
        if (channels < 1 || channels > 32)
            throw new IllegalArgumentException("Encoder panel must have 1 to 32 channels");
        if (patchSize != 32 && patchSize != 64 && patchSize != 128 && patchSize != 256)
            throw new IllegalArgumentException("Encoder image size must be 32, 64, 128 or 256");
        elements = Math.multiplyExact(channels, patchSize * patchSize);
        input = ByteBuffer.allocateDirect(elements * Float.BYTES).order(ByteOrder.nativeOrder());
        Device selectedDevice = selectDevice(device);
        System.err.println("Native KRONOS execution device: " + selectedDevice
                + "; requested: " + device);
        model = Model.newInstance("mif-frozen-encoder", selectedDevice, "PyTorch");
        try {
            model.load(weights.getParent(), weights.getFileName().toString());
            predictor = model.newPredictor(new Translator<>() {
                @Override public NDList processInput(TranslatorContext context, ByteBuffer raw) {
                    return new NDList(context.getNDManager().create(raw,
                            new Shape(1, channels, patchSize, patchSize), DataType.FLOAT32));
                }
                @Override public float[] processOutput(TranslatorContext context, NDList output) {
                    var features = output.singletonOrThrow();
                    if (!features.getShape().equals(new Shape(1, 768)))
                        throw new IllegalStateException("Frozen encoder returned an unexpected feature shape");
                    float[] result = features.toFloatArray();
                    for (float value : result)
                        if (!Float.isFinite(value))
                            throw new IllegalStateException("Frozen encoder returned nonfinite features");
                    return result;
                }
                @Override public Batchifier getBatchifier() { return null; }
            });
        } catch (Exception failure) {
            model.close();
            throw failure;
        }
    }

    private static Device selectDevice(String requested) {
        NativeModelRuntime.prepareDevice(requested);
        if (requested.equals("cpu")) return Device.cpu();
        if (!requested.equals("auto") && !requested.equals("cuda") && !requested.startsWith("gpu"))
            throw new IllegalArgumentException("Native KRONOS requires CPU or CUDA");
        if (Engine.getEngine("PyTorch").getGpuCount() > 0) {
            Device gpu = requested.startsWith("gpu") ? Device.fromName(requested) : Device.gpu();
            var memory = CudaUtils.getGpuMemory(gpu);
            String allowance = System.getenv("MIF_DEVICE_MEMORY_BYTES");
            long required = allowance == null ? 2560L * 1024 * 1024 : Long.parseLong(allowance);
            long total = memory.getMax(), used = memory.getUsed();
            if (required > 0 && total > 0 && used >= 0 && required <= total * 4 / 5 - used
                    && required <= (total - used) * 4 / 5) return gpu;
        }
        if (requested.equals("auto")) return Device.cpu();
        throw new IllegalStateException("The requested CUDA device is unavailable or its memory allowance is insufficient");
    }

    /** Predictor access and buffer reuse belong to the same serial session. */
    public synchronized float[] encode(float[] raw) throws Exception {
        if (closed) throw new IllegalStateException("Frozen encoder session is closed");
        if (Thread.currentThread().isInterrupted())
            throw new InterruptedException("Frozen encoding cancelled before input allocation");
        if (raw.length != elements)
            throw new IllegalArgumentException("Encoder input differs from its frozen panel and patch dimensions");
        for (float value : raw)
            if (!Float.isFinite(value))
                throw new IllegalArgumentException("Encoder input contains nonfinite or unrepresentable intensities");
        input.clear();
        input.asFloatBuffer().put(raw);
        calls++;
        float[] result = predictor.predict(input);
        if (Thread.currentThread().isInterrupted())
            throw new InterruptedException("Frozen encoding cancelled after native prediction completed");
        return result;
    }

    public synchronized long getInferenceCount() { return calls; }
    public long getHostBufferBytes() { return (long) elements * Float.BYTES; }

    @Override public synchronized void close() {
        if (!closed) {
            closed = true;
            predictor.close();
            model.close();
        }
    }
}
