package com.backupx.file;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.Arrays;

public class FileChunker {

    // Default chunk size set to 1MB
    public static final int DEFAULT_CHUNK_SIZE = 1024 * 1024;

    @FunctionalInterface
    public interface ChunkConsumer {
        void accept(long offset, byte[] chunkData);
    }

    public static long forEachChunk(Path filePath, int chunkSize, ChunkConsumer consumer) throws IOException {
        if (chunkSize <= 0) {
            throw new IllegalArgumentException("chunkSize must be greater than zero");
        }

        long totalBytesProcessed = 0;
        try (InputStream is = Files.newInputStream(filePath, StandardOpenOption.READ)) {
            byte[] buffer = new byte[chunkSize];
            int bytesRead;

            while ((bytesRead = is.read(buffer)) != -1) {
                byte[] actualChunk = (bytesRead == chunkSize) ? buffer : Arrays.copyOf(buffer, bytesRead);
                consumer.accept(totalBytesProcessed, actualChunk);
                totalBytesProcessed += bytesRead;
            }
        }
        return totalBytesProcessed;
    }
}