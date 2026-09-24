package com.corelink;

import java.awt.Color;
import java.util.List;

/**
 * Two tenants running the same vendor product ("CoreLink") at different versions, with
 * different branding, labels, and one extra step. Stand-in for multi-tenant drift.
 */
public record TenantProfile(
        String id,
        String institutionName,
        String productVersion,
        String memberNumberLabel,
        String inquireButtonText,
        Color bannerColor,
        List<String> subAccountTypes,
        boolean requiresDisclosureAck) {

    public static TenantProfile forId(String id) {
        return switch (id) {
            case "lakeshore" -> new TenantProfile(
                    "lakeshore", "Lakeshore Community Credit Union", "CoreLink 7.6.2",
                    "Account No.:", "Search", new Color(0x2E, 0x5E, 0x3A),
                    List.of("Share Certificate", "Money Market", "Holiday Club", "Youth Savings"),
                    true);
            default -> new TenantProfile(
                    "heritage", "Heritage Federal Credit Union", "CoreLink 7.4.1",
                    "Member #:", "Inquire", new Color(0x1F, 0x3A, 0x68),
                    List.of("Share Certificate", "Money Market", "Christmas Club"),
                    false);
        };
    }
}
