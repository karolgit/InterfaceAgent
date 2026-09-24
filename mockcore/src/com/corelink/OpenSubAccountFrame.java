package com.corelink;

import java.awt.BorderLayout;
import java.awt.CardLayout;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.GridLayout;
import java.awt.Insets;
import java.math.BigDecimal;
import java.text.NumberFormat;
import java.util.Locale;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JComboBox;
import javax.swing.JComponent;
import javax.swing.JInternalFrame;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JPasswordField;
import javax.swing.JTextField;

/**
 * Open Sub-Account: entry, then review, then post (irreversible), then confirmation.
 *
 * Labels here are intentionally NOT linked to their fields with setLabelFor, as in many real
 * legacy apps, so the accessibility tree gives the fields no names. Automation has to find
 * them by label proximity or position.
 *
 * Exercises: not-found, restricted member, validation (amount, minimum, insufficient funds),
 * supervisor override above the operator limit, a tenant-specific disclosure dialog,
 * an irreversible-action confirmation, permission denial, and host error.
 */
final class OpenSubAccountFrame extends JInternalFrame {
    private static final NumberFormat USD = NumberFormat.getCurrencyInstance(Locale.US);
    private static final BigDecimal OPERATOR_LIMIT = new BigDecimal("10000.00");

    private final MainFrame app;
    private final CardLayout cards = new CardLayout();
    private final JPanel deck = new JPanel(cards);

    private final JTextField memberField = new JTextField(10);
    private final JLabel memberName = new JLabel(" ");
    private final JComboBox<String> typeBox;
    private final JTextField amountField = new JTextField(10);
    private final JComboBox<MemberStore.Account> sourceBox = new JComboBox<>();
    private final JLabel reviewText = new JLabel();
    private final JLabel confText = new JLabel();

    private MemberStore.Member member;
    private BigDecimal amount;

    OpenSubAccountFrame(MainFrame app) {
        super("Open Sub-Account  [TRAN 0417]", true, true, false, true);
        this.app = app;
        typeBox = new JComboBox<>(app.tenant.subAccountTypes().toArray(new String[0]));
        deck.add(buildEntry(), "entry");
        deck.add(buildReview(), "review");
        deck.add(buildDone(), "done");
        add(deck, BorderLayout.CENTER);
        setSize(560, 340);
    }

    private JPanel buildEntry() {
        JPanel p = new JPanel(new GridBagLayout());
        p.setBorder(BorderFactory.createTitledBorder("New Share Sub-Account"));
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(5, 8, 5, 8);
        c.anchor = GridBagConstraints.WEST;
        memberField.setFont(MainFrame.MONO);
        amountField.setFont(MainFrame.MONO);
        memberName.setFont(MainFrame.MONO_BOLD);
        JButton load = new JButton("Load");
        JButton review = new JButton("Review >>");

        row(p, c, 0, "Member Number", memberField, load);
        row(p, c, 1, "Member Name", memberName, null);
        row(p, c, 2, "Account Type", typeBox, null);
        row(p, c, 3, "Initial Deposit", amountField, null);
        row(p, c, 4, "Fund From", sourceBox, null);
        c.gridx = 1; c.gridy = 5; p.add(review, c);

        load.addActionListener(e -> app.guard(this::loadMember));
        memberField.addActionListener(e -> app.guard(this::loadMember));
        review.addActionListener(e -> app.guard(this::toReview));
        return p;
    }

    private static void row(JPanel p, GridBagConstraints c, int y, String label,
                            JComponent field, JComponent extra) {
        c.gridy = y;
        c.gridx = 0; p.add(new JLabel(label), c); // deliberately no setLabelFor
        c.gridx = 1; p.add(field, c);
        if (extra != null) { c.gridx = 2; p.add(extra, c); }
    }

    private JPanel buildReview() {
        JPanel p = new JPanel(new BorderLayout());
        p.setBorder(BorderFactory.createTitledBorder("Review Transaction"));
        reviewText.setFont(MainFrame.MONO);
        reviewText.setVerticalAlignment(JLabel.TOP);
        p.add(reviewText, BorderLayout.CENTER);
        JPanel buttons = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        JButton back = new JButton("<< Back");
        JButton post = new JButton("Post Transaction");
        buttons.add(back);
        buttons.add(post);
        p.add(buttons, BorderLayout.SOUTH);
        back.addActionListener(e -> cards.show(deck, "entry"));
        post.addActionListener(e -> app.guard(this::post));
        return p;
    }

    private JPanel buildDone() {
        JPanel p = new JPanel(new BorderLayout());
        p.setBorder(BorderFactory.createTitledBorder("Transaction Complete"));
        JLabel head = new JLabel("TRANSACTION POSTED", JLabel.CENTER);
        head.setFont(new Font(Font.MONOSPACED, Font.BOLD, 20));
        confText.setFont(MainFrame.MONO_BOLD);
        confText.setHorizontalAlignment(JLabel.CENTER);
        JPanel mid = new JPanel(new GridLayout(2, 1));
        mid.add(head);
        mid.add(confText);
        p.add(mid, BorderLayout.CENTER);
        JButton again = new JButton("New Transaction");
        again.addActionListener(e -> resetForm());
        JPanel s = new JPanel(new FlowLayout(FlowLayout.RIGHT));
        s.add(again);
        p.add(s, BorderLayout.SOUTH);
        return p;
    }

    private void resetForm() {
        member = null;
        amount = null;
        memberField.setText("");
        memberName.setText(" ");
        amountField.setText("");
        sourceBox.removeAllItems();
        cards.show(deck, "entry");
    }

    private void loadMember() {
        member = null;
        memberName.setText(" ");
        sourceBox.removeAllItems();
        var found = app.store.find(memberField.getText());
        if (found.isEmpty()) {
            app.setStatus("MBR NOT ON FILE", true);
            return;
        }
        MemberStore.Member m = found.get();
        if (m.status() != MemberStore.Status.ACTIVE) {
            app.setStatus("MEMBER " + m.status() + " - TRANSACTION NOT ALLOWED", true);
            memberName.setText(m.name() + "  [" + m.status() + "]");
            return;
        }
        member = m;
        memberName.setText(m.name());
        for (MemberStore.Account a : m.accounts()) sourceBox.addItem(a);
        app.setStatus("MEMBER LOADED", false);
    }

    private void toReview() {
        if (member == null) {
            app.setStatus("LOAD A MEMBER FIRST", true);
            return;
        }
        String type = (String) typeBox.getSelectedItem();
        MemberStore.Account src = (MemberStore.Account) sourceBox.getSelectedItem();
        BigDecimal amt;
        try {
            amt = new BigDecimal(amountField.getText().trim().replace("$", "").replace(",", ""));
        } catch (NumberFormatException e) {
            app.setStatus("INVALID AMOUNT", true);
            return;
        }
        if (amt.signum() <= 0) {
            app.setStatus("INVALID AMOUNT", true);
            return;
        }
        BigDecimal min = app.store.minimumDeposit(type);
        if (amt.compareTo(min) < 0) {
            app.setStatus("MINIMUM OPENING DEPOSIT FOR " + type.toUpperCase() + " IS " + USD.format(min), true);
            return;
        }
        if (src == null || src.balance.compareTo(amt) < 0) {
            app.setStatus("INSUFFICIENT FUNDS IN SOURCE ACCOUNT " + (src == null ? "" : src.suffix), true);
            return;
        }
        if (amt.compareTo(OPERATOR_LIMIT) > 0 && !supervisorOverride()) {
            app.setStatus("AMOUNT EXCEEDS OPERATOR LIMIT - SUPERVISOR OVERRIDE REQUIRED", true);
            return;
        }
        if (app.tenant.requiresDisclosureAck()) {
            int ack = JOptionPane.showConfirmDialog(this,
                    "Has the member received the Truth-in-Savings disclosure for this product?",
                    "Disclosure Acknowledgement", JOptionPane.YES_NO_OPTION);
            if (ack != JOptionPane.YES_OPTION) {
                app.setStatus("DISCLOSURE NOT ACKNOWLEDGED", true);
                return;
            }
        }
        amount = amt;
        reviewText.setText("<html><pre>"
                + "MEMBER:      " + member.number() + "  " + member.name() + "\n"
                + "NEW PRODUCT: " + type + "\n"
                + "DEPOSIT:     " + USD.format(amt) + "\n"
                + "FUND FROM:   " + src.suffix + " " + src.type + "\n"
                + "SRC BAL AFT: " + USD.format(src.balance.subtract(amt)) + "\n"
                + "</pre></html>");
        cards.show(deck, "review");
        app.setStatus("REVIEW TRANSACTION AND POST", false);
    }

    /** Requires a supervisor to key their own credentials. Fake supervisor: sup01 / demo123. */
    private boolean supervisorOverride() {
        JTextField id = new JTextField(10);
        JPasswordField pw = new JPasswordField(10);
        JPanel p = new JPanel(new GridLayout(3, 2, 4, 4));
        p.add(new JLabel("AMOUNT EXCEEDS OPERATOR LIMIT"));
        p.add(new JLabel(""));
        p.add(new JLabel("Supervisor ID:"));
        p.add(id);
        p.add(new JLabel("Password:"));
        p.add(pw);
        int r = JOptionPane.showConfirmDialog(this, p, "Supervisor Override Required",
                JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE);
        return r == JOptionPane.OK_OPTION
                && "sup01".equalsIgnoreCase(id.getText().trim())
                && "demo123".equals(new String(pw.getPassword()));
    }

    private void post() {
        if (Faults.on("permission_denied")) {
            app.setStatus("OPERATOR NOT AUTHORIZED FOR TRAN CODE 0417", true);
            return;
        }
        int ok = JOptionPane.showConfirmDialog(this,
                "POST TRANSACTION?\nTHIS ACTION CANNOT BE REVERSED.",
                "Confirm Posting", JOptionPane.YES_NO_OPTION, JOptionPane.WARNING_MESSAGE);
        if (ok != JOptionPane.YES_OPTION) {
            app.setStatus("POSTING CANCELLED", false);
            return;
        }
        if (Faults.on("app_error")) {
            app.setStatus("HOST COMMUNICATION FAILURE", true);
            JOptionPane.showMessageDialog(this,
                    "HOST COMMUNICATION FAILURE (ERR 0x2F1)\nTRANSACTION NOT POSTED",
                    "System Error", JOptionPane.ERROR_MESSAGE);
            return;
        }
        String type = (String) typeBox.getSelectedItem();
        MemberStore.Account src = (MemberStore.Account) sourceBox.getSelectedItem();
        String conf = app.store.openSubAccount(member, type, src, amount);
        confText.setText("CONFIRMATION #: " + conf);
        cards.show(deck, "done");
        app.setStatus("TRANSACTION POSTED - CONF " + conf, false);
    }
}
