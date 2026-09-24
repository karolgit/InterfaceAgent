package com.corelink;

/** Operator session with a real idle timeout plus a forced-expiry fault. */
public final class Session {
    public enum Role { TELLER, INQUIRY_ONLY }

    private static final long IDLE_MS = Long.getLong("corelink.idleTimeoutSec", 900L) * 1000L;

    private String operator;
    private Role role;
    private long lastActivity;

    /** Fake operators only: teller01 and viewer01, password demo123. */
    public boolean signOn(String user, String password) {
        if (!"demo123".equals(password)) return false;
        switch (user.trim().toLowerCase()) {
            case "teller01" -> role = Role.TELLER;
            case "viewer01" -> role = Role.INQUIRY_ONLY;
            default -> {
                return false;
            }
        }
        operator = user.trim().toUpperCase();
        touch();
        return true;
    }

    public void signOff() {
        operator = null;
        role = null;
    }

    /** True if the session has expired through idle timeout or the injected fault. */
    public boolean expired() {
        return operator == null
                || System.currentTimeMillis() - lastActivity > IDLE_MS
                || Faults.once("session_expired");
    }

    public void touch() { lastActivity = System.currentTimeMillis(); }

    public String operator() { return operator; }

    public Role role() { return role; }
}
