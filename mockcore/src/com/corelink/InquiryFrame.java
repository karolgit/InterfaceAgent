package com.corelink;

import java.awt.BorderLayout;
import java.awt.Cursor;
import java.awt.FlowLayout;
import java.awt.GridLayout;
import java.text.NumberFormat;
import java.util.Locale;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JInternalFrame;
import javax.swing.JLabel;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JScrollPane;
import javax.swing.JTable;
import javax.swing.JTextField;
import javax.swing.Timer;
import javax.swing.table.DefaultTableModel;

/**
 * Member inquiry: search by member number, show demographics and share accounts.
 * Exercises: validation, not-found, restricted and closed members, slow load, broadcast popup,
 * host error, and session expiry (through MainFrame.guard).
 */
final class InquiryFrame extends JInternalFrame {
    private static final NumberFormat USD = NumberFormat.getCurrencyInstance(Locale.US);

    private final MainFrame app;
    private final JTextField memberField = new JTextField(10);
    private final JButton inquire;
    private final JLabel name = value();
    private final JLabel status = value();
    private final JLabel ssn = value();
    private final JLabel dob = value();
    private final JLabel address = value();
    private final DefaultTableModel model =
            new DefaultTableModel(new Object[] {"Suffix", "Description", "Balance", "Available"}, 0) {
                @Override
                public boolean isCellEditable(int r, int c) { return false; }
            };

    InquiryFrame(MainFrame app) {
        super("Member Inquiry", true, true, true, true);
        this.app = app;
        inquire = new JButton(app.tenant.inquireButtonText());

        JPanel search = new JPanel(new FlowLayout(FlowLayout.LEFT));
        JLabel memberLbl = new JLabel(app.tenant.memberNumberLabel());
        memberLbl.setLabelFor(memberField);
        memberField.setFont(MainFrame.MONO);
        search.add(memberLbl);
        search.add(memberField);
        search.add(inquire);

        JPanel info = new JPanel(new GridLayout(5, 2, 4, 2));
        info.setBorder(BorderFactory.createTitledBorder("Member Information"));
        info.add(new JLabel("Name:")); info.add(name);
        info.add(new JLabel("Status:")); info.add(status);
        info.add(new JLabel("SSN:")); info.add(ssn);
        info.add(new JLabel("Date of Birth:")); info.add(dob);
        info.add(new JLabel("Address:")); info.add(address);

        JTable table = new JTable(model);
        table.setFont(MainFrame.MONO);
        JScrollPane scroll = new JScrollPane(table);
        scroll.setBorder(BorderFactory.createTitledBorder("Share Accounts"));

        JPanel center = new JPanel(new BorderLayout());
        center.add(info, BorderLayout.NORTH);
        center.add(scroll, BorderLayout.CENTER);

        add(search, BorderLayout.NORTH);
        add(center, BorderLayout.CENTER);

        inquire.addActionListener(e -> app.guard(this::runInquiry));
        memberField.addActionListener(e -> app.guard(this::runInquiry));
        setSize(620, 420);
    }

    private static JLabel value() {
        JLabel l = new JLabel(" ");
        l.setFont(MainFrame.MONO_BOLD);
        return l;
    }

    private void clear() {
        for (JLabel l : new JLabel[] {name, status, ssn, dob, address}) l.setText(" ");
        model.setRowCount(0);
    }

    private void runInquiry() {
        clear();
        String number = memberField.getText().trim();
        if (!number.matches("\\d{5}")) {
            app.setStatus("INVALID MEMBER NUMBER FORMAT - 5 DIGITS REQUIRED", true);
            return;
        }
        if (Faults.on("app_error")) {
            app.setStatus("HOST COMMUNICATION FAILURE", true);
            JOptionPane.showMessageDialog(this,
                    "HOST COMMUNICATION FAILURE (ERR 0x2F1)\nCONTACT HELP DESK EXT 4400",
                    "System Error", JOptionPane.ERROR_MESSAGE);
            return;
        }
        int delay = Faults.slowMs();
        if (delay > 0) {
            inquire.setEnabled(false);
            setCursor(Cursor.getPredefinedCursor(Cursor.WAIT_CURSOR));
            app.setStatus("PROCESSING...", false);
            Timer t = new Timer(delay, e -> {
                inquire.setEnabled(true);
                setCursor(Cursor.getDefaultCursor());
                finishInquiry(number);
            });
            t.setRepeats(false);
            t.start();
        } else {
            finishInquiry(number);
        }
    }

    private void finishInquiry(String number) {
        if (Faults.on("popup")) {
            JOptionPane.showMessageDialog(this,
                    "BROADCAST MESSAGE\nEnd-of-day processing begins at 18:00 ET.\nPlease complete open transactions.",
                    "System Broadcast", JOptionPane.INFORMATION_MESSAGE);
        }
        var found = app.store.find(number);
        if (found.isEmpty()) {
            app.setStatus("MBR NOT ON FILE", true);
            return;
        }
        MemberStore.Member m = found.get();
        name.setText(m.name());
        status.setText(m.status().name());
        ssn.setText(m.ssn());
        dob.setText(m.dob());
        address.setText(m.address());
        switch (m.status()) {
            case RESTRICTED -> {
                model.addRow(new Object[] {"***", "*** RESTRICTED - SEE SUPERVISOR ***", "", ""});
                app.setStatus("MEMBER RESTRICTED - SUPERVISOR REVIEW REQUIRED", true);
            }
            case CLOSED -> app.setStatus("MEMBERSHIP CLOSED", true);
            default -> {
                for (MemberStore.Account a : m.accounts()) {
                    String bal = USD.format(a.balance);
                    model.addRow(new Object[] {a.suffix, a.type, bal, bal});
                }
                app.setStatus("INQUIRY COMPLETE - " + m.accounts().size() + " SHARE(S)", false);
            }
        }
    }
}
