package com.corelink;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/** In-memory fake member data. SSNs use the 900 series, which is never issued. */
public final class MemberStore {
    public enum Status { ACTIVE, RESTRICTED, CLOSED }

    public static final class Account {
        public final String suffix;
        public final String type;
        public BigDecimal balance;

        Account(String suffix, String type, String balance) {
            this.suffix = suffix;
            this.type = type;
            this.balance = new BigDecimal(balance);
        }

        @Override
        public String toString() { return suffix + " " + type; }
    }

    public record Member(String number, String name, String ssn, String dob, String address,
                         Status status, List<Account> accounts) {}

    private final Map<String, Member> members = new LinkedHashMap<>();
    private int confSeq = 104_220;

    public MemberStore() {
        add(new Member("12345", "SAMPLE, JANE Q", "900-12-3456", "03/14/1978",
                "100 TEST ST, SPRINGFIELD", Status.ACTIVE, new ArrayList<>(List.of(
                new Account("S01", "Share Savings", "4210.55"),
                new Account("S10", "Share Draft Checking", "1893.20")))));
        add(new Member("23456", "TESTPERSON, ROBERT", "900-98-7654", "11/02/1990",
                "22 EXAMPLE AVE, SHELBYVILLE", Status.ACTIVE, new ArrayList<>(List.of(
                new Account("S01", "Share Savings", "25.00"),
                new Account("S10", "Share Draft Checking", "15320.00"),
                new Account("S20", "Money Market", "50000.00")))));
        add(new Member("34567", "DEMO, ALEX", "900-55-1212", "07/30/1965",
                "9 MOCK RD, OGDENVILLE", Status.RESTRICTED, new ArrayList<>(List.of(
                new Account("S01", "Share Savings", "310.00")))));
        add(new Member("45678", "PLACEHOLDER, SAM", "900-44-0000", "01/01/1950",
                "1 FAKE LN, NORTH HAVERBROOK", Status.CLOSED, new ArrayList<>()));
    }

    private void add(Member m) { members.put(m.number(), m); }

    public Optional<Member> find(String number) {
        return Optional.ofNullable(members.get(number == null ? "" : number.trim()));
    }

    public BigDecimal minimumDeposit(String type) {
        return switch (type) {
            case "Share Certificate" -> new BigDecimal("500.00");
            case "Money Market" -> new BigDecimal("2500.00");
            default -> new BigDecimal("5.00");
        };
    }

    /** Irreversible: debits the source and creates the sub-account. Returns a confirmation number. */
    public String openSubAccount(Member m, String type, Account source, BigDecimal amount) {
        source.balance = source.balance.subtract(amount);
        String suffix = String.format("S%02d", 30 + m.accounts().size());
        m.accounts().add(new Account(suffix, type, amount.toPlainString()));
        return "CL-" + (++confSeq) + "-" + suffix;
    }
}
