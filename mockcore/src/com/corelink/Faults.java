package com.corelink;

import java.io.IOException;
import java.io.Reader;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Properties;

/**
 * Runtime fault injection. Re-reads faults.properties on every call so a test harness can
 * flip faults on and off while the app is running. A missing file means no faults.
 *
 * Keys: slow_ms=4000, popup=true, session_expired=true, app_error=true, permission_denied=true
 */
public final class Faults {
    private static final Path FILE =
            Path.of(System.getProperty("corelink.faults", "faults.properties"));

    private Faults() {}

    private static Properties load() {
        Properties p = new Properties();
        if (Files.exists(FILE)) {
            try (Reader r = Files.newBufferedReader(FILE)) {
                p.load(r);
            } catch (IOException ignored) {
                // treat an unreadable file as no faults
            }
        }
        return p;
    }

    public static boolean on(String key) {
        return Boolean.parseBoolean(load().getProperty(key, "false").trim());
    }

    private static final java.util.Map<String, Long> firedAt = new java.util.HashMap<>();

    /**
     * One-shot fault: fires at most once per edit of the faults file. Lets a harness inject
     * a single session expiry that a replay can recover from, instead of an endless loop.
     */
    public static boolean once(String key) {
        if (!on(key)) return false;
        long mtime;
        try {
            mtime = Files.getLastModifiedTime(FILE).toMillis();
        } catch (IOException e) {
            return false;
        }
        Long prev = firedAt.put(key, mtime);
        return prev == null || prev != mtime;
    }

    public static int slowMs() {
        try {
            return Integer.parseInt(load().getProperty("slow_ms", "0").trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }
}
