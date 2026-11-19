package org.mif.io;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;

/** Materialize accepted model bytes inside the native operation's target project. */
public final class NativeClassifierFiles {
    // Match the fitted-model contract; verification and copying stream the file.
    private static final long MAX_MODEL_BYTES = 64L * 1024 * 1024 * 1024;
    private NativeClassifierFiles() {}

    public static String ensureModel(Path projectRoot, String member, String sourceModelPath,
            String expectedSha256, long expectedBytes) throws IOException {
        if (member == null || !member.matches("classifiers/pixel_classifiers/mif-[0-9a-f]{64}\\.json")
                || expectedSha256 == null || !expectedSha256.matches("[0-9a-f]{64}")
                || expectedBytes <= 0 || expectedBytes > MAX_MODEL_BYTES
                || !projectRoot.isAbsolute() || !projectRoot.equals(projectRoot.normalize())
                || !projectRoot.toRealPath().equals(projectRoot))
            throw new IOException("Accepted native classifier material is invalid");
        Path target = projectRoot.resolve(member);
        if (sourceModelPath != null) {
            Path source = Path.of(sourceModelPath);
            if (!source.isAbsolute() || !source.equals(source.normalize())
                    || !source.getFileName().toString().equals(expectedSha256 + ".json")
                    || !source.getParent().getFileName().toString().equals("models"))
                throw new IOException("Classifier import is not its admitted export model member");
            try (var input = new PinnedInputFile(source.getParent(), source.getFileName().toString())) {
                if (input.getLength() != expectedBytes)
                    throw new IOException("Classifier import size differs from accepted bytes");
                for (String component : new String[]{"classifiers", "pixel_classifiers"}) {
                    Path directory = component.equals("classifiers")
                        ? projectRoot.resolve(component) : projectRoot.resolve("classifiers").resolve(component);
                    try { Files.createDirectory(directory); }
                    catch (java.nio.file.FileAlreadyExistsException existing) {
                        if (!Files.isDirectory(directory, LinkOption.NOFOLLOW_LINKS)) throw existing;
                    }
                    if (!directory.toRealPath().equals(directory))
                        throw new IOException("Classifier destination directory is redirected");
                }
                if (!Files.exists(target, LinkOption.NOFOLLOW_LINKS)) {
                    input.copyNewTo(target, expectedSha256, expectedBytes);
                } else {
                    // Later domains reuse the same completed model, while the admitted
                    // source remains exact. We never overwrite an occupied member.
                    if (!input.sha256().equals(expectedSha256))
                        throw new IOException("Classifier import differs from accepted bytes");
                }
                input.validate();
            }
        }
        try (var retained = new PinnedInputFile(projectRoot, member)) {
            String actual = retained.sha256();
            if (retained.getLength() != expectedBytes || !actual.equals(expectedSha256))
                throw new IOException("Target classifier differs from accepted bytes");
            retained.validate();
            return actual;
        }
    }
}
