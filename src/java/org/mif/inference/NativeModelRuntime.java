package org.mif.inference;

import ai.djl.util.Utils;
import java.nio.file.Files;
import java.nio.file.Path;

/** Select installed tensor libraries before DJL initializes a model engine. */
public final class NativeModelRuntime {
    private NativeModelRuntime() {}

    /**
     * The application launcher selects the complete installed CUDA library set
     * when available; that build also supports CPU calculations. Otherwise we
     * connect CPU requests to the managed CPU directory. A GPU request without
     * installed CUDA libraries returns a setup correction instead of downloading
     * dependencies while an analysis is running. Explicit DJL selections retain
     * precedence, including the isolated CPU prerequisite probe.
     */
    public static synchronized void prepareDevice(String requested) {
        String operatingSystem = System.getProperty("os.name");
        if (!operatingSystem.startsWith("Linux") && !operatingSystem.startsWith("Windows")) return;
        if (!requested.equals("cpu") && !requested.equals("auto")
                && !requested.startsWith("cuda") && !requested.startsWith("gpu")) return;
        System.setProperty("DJL_OFFLINE", "true");
        if (Utils.getEnvOrSystemProperty("PYTORCH_LIBRARY_PATH") != null
                || Utils.getEnvOrSystemProperty("PYTORCH_FLAVOR") != null) return;
        if (requested.startsWith("cuda") || requested.startsWith("gpu")) {
            throw new IllegalStateException(
                    "NVIDIA inference libraries are unavailable. Open Software setup, "
                    + "install the QuPath GPU bridge in the NVIDIA application, then retry. "
                    + "Choose CPU in Computing to use installed CPU libraries.");
        }
        String selected = System.getenv("MIF_CPU_NATIVE_ROOT");
        if (selected == null || !Path.of(selected).isAbsolute()
                || !Files.isDirectory(Path.of(selected))) {
            throw new IllegalStateException(
                    "CPU inference libraries are unavailable. Open Software setup, "
                    + "install the cell-segmentation libraries, then retry this step.");
        }
        System.setProperty("PYTORCH_LIBRARY_PATH", selected);
        System.setProperty("PYTORCH_FLAVOR", "cpu");
    }
}
