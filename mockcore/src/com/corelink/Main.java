package com.corelink;

import javax.swing.SwingUtilities;
import javax.swing.UIManager;
import javax.swing.plaf.metal.DefaultMetalTheme;
import javax.swing.plaf.metal.MetalLookAndFeel;

/**
 * CoreLink mock core-banking client. A deliberately legacy-style Swing MDI app used as the
 * proxy target for the computer-use system. All data is fake.
 *
 * Usage: java -cp out com.corelink.Main [--tenant=heritage|lakeshore]
 * Faults: edit ./faults.properties while the app runs (it is re-read on every action).
 */
public final class Main {
    private Main() {}

    public static void main(String[] args) {
        String tenantId = System.getProperty("corelink.tenant", "heritage");
        for (String a : args) {
            if (a.startsWith("--tenant=")) tenantId = a.substring("--tenant=".length());
        }
        TenantProfile tenant = TenantProfile.forId(tenantId);
        SwingUtilities.invokeLater(() -> {
            try {
                MetalLookAndFeel.setCurrentTheme(new DefaultMetalTheme()); // old grey Metal look
                UIManager.setLookAndFeel(new MetalLookAndFeel());
            } catch (Exception ignored) {
                // fall back to the platform default look
            }
            new MainFrame(tenant, new MemberStore(), new Session()).start();
        });
    }
}
