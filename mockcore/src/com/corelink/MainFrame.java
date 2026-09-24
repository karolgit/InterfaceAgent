package com.corelink;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Dimension;
import java.awt.FlowLayout;
import java.awt.Font;
import java.awt.event.KeyEvent;
import java.beans.PropertyVetoException;
import javax.swing.BorderFactory;
import javax.swing.JButton;
import javax.swing.JDesktopPane;
import javax.swing.JFrame;
import javax.swing.JInternalFrame;
import javax.swing.JLabel;
import javax.swing.JMenu;
import javax.swing.JMenuBar;
import javax.swing.JMenuItem;
import javax.swing.JOptionPane;
import javax.swing.JPanel;
import javax.swing.JToolBar;
import javax.swing.KeyStroke;

/** MDI shell: banner, menus, toolbar, desktop pane, and a status bar. */
public final class MainFrame extends JFrame {
    static final Font MONO = new Font(Font.MONOSPACED, Font.PLAIN, 13);
    static final Font MONO_BOLD = new Font(Font.MONOSPACED, Font.BOLD, 13);

    final TenantProfile tenant;
    final MemberStore store;
    final Session session;

    private final JDesktopPane desktop = new JDesktopPane();
    private final JLabel statusMsg = new JLabel(" READY");
    private final JLabel statusInfo = new JLabel();

    MainFrame(TenantProfile tenant, MemberStore store, Session session) {
        super(tenant.productVersion() + " - " + tenant.institutionName());
        this.tenant = tenant;
        this.store = store;
        this.session = session;
        setDefaultCloseOperation(JFrame.EXIT_ON_CLOSE);
        setSize(1024, 720);
        setLocationRelativeTo(null);

        JLabel banner = new JLabel("  " + tenant.institutionName().toUpperCase()
                + "   |   MEMBER SERVICES");
        banner.setOpaque(true);
        banner.setBackground(tenant.bannerColor());
        banner.setForeground(Color.WHITE);
        banner.setFont(new Font(Font.SANS_SERIF, Font.BOLD, 16));
        banner.setPreferredSize(new Dimension(0, 34));

        JToolBar toolbar = new JToolBar();
        toolbar.setFloatable(false);
        JButton tbInquiry = new JButton("F2 Inquiry");
        tbInquiry.addActionListener(e -> guard(this::openInquiry));
        JButton tbOpen = new JButton("F5 Open Acct");
        tbOpen.addActionListener(e -> guard(this::openSubAccount));
        toolbar.add(tbInquiry);
        toolbar.add(tbOpen);

        JPanel north = new JPanel(new BorderLayout());
        north.add(banner, BorderLayout.NORTH);
        north.add(toolbar, BorderLayout.SOUTH);

        desktop.setBackground(new Color(0x70, 0x80, 0x90));

        JPanel status = new JPanel(new BorderLayout());
        status.setBorder(BorderFactory.createLoweredBevelBorder());
        statusMsg.setFont(MONO_BOLD);
        statusInfo.setFont(MONO);
        status.add(statusMsg, BorderLayout.CENTER);
        JPanel right = new JPanel(new FlowLayout(FlowLayout.RIGHT, 6, 0));
        right.add(statusInfo);
        status.add(right, BorderLayout.EAST);

        setJMenuBar(buildMenu());
        add(north, BorderLayout.NORTH);
        add(desktop, BorderLayout.CENTER);
        add(status, BorderLayout.SOUTH);
    }

    private JMenuBar buildMenu() {
        JMenuBar bar = new JMenuBar();
        JMenu file = new JMenu("File");
        JMenuItem signOff = new JMenuItem("Sign Off");
        signOff.addActionListener(e -> signOffAndPrompt());
        JMenuItem exit = new JMenuItem("Exit");
        exit.addActionListener(e -> System.exit(0));
        file.add(signOff);
        file.add(exit);

        JMenu member = new JMenu("Member");
        JMenuItem inquiry = new JMenuItem("Member Inquiry");
        inquiry.setAccelerator(KeyStroke.getKeyStroke(KeyEvent.VK_F2, 0));
        inquiry.addActionListener(e -> guard(this::openInquiry));
        member.add(inquiry);

        JMenu accounts = new JMenu("Accounts");
        JMenuItem open = new JMenuItem("Open Sub-Account");
        open.setAccelerator(KeyStroke.getKeyStroke(KeyEvent.VK_F5, 0));
        open.addActionListener(e -> guard(this::openSubAccount));
        accounts.add(open);

        JMenu help = new JMenu("Help");
        JMenuItem about = new JMenuItem("About");
        about.addActionListener(e -> JOptionPane.showMessageDialog(this,
                tenant.productVersion() + "\n(c) 1998-2019 CoreLink Systems Inc.\nMOCK SOFTWARE - FAKE DATA",
                "About", JOptionPane.INFORMATION_MESSAGE));
        help.add(about);

        bar.add(file);
        bar.add(member);
        bar.add(accounts);
        bar.add(help);
        return bar;
    }

    void start() {
        setVisible(true);
        promptSignOn();
    }

    private void promptSignOn() {
        statusInfo.setText("OPR: ------  BR: 001  " + tenant.productVersion());
        new LoginDialog(this, session).setVisible(true); // modal
        if (session.operator() == null) System.exit(0);
        statusInfo.setText("OPR: " + session.operator() + "  BR: 001  " + tenant.productVersion());
        setStatus("SIGNED ON AS " + session.operator(), false);
    }

    private void signOffAndPrompt() {
        closeAllWindows();
        session.signOff();
        setStatus("SIGNED OFF", false);
        promptSignOn();
    }

    /** Every operator action goes through here: enforces session expiry, then records activity. */
    void guard(Runnable action) {
        if (session.expired()) {
            JOptionPane.showMessageDialog(this,
                    "SESSION EXPIRED - PLEASE SIGN ON AGAIN",
                    "CoreLink", JOptionPane.WARNING_MESSAGE);
            signOffAndPrompt();
            return;
        }
        session.touch();
        action.run();
    }

    private static final java.time.format.DateTimeFormatter HMS =
            java.time.format.DateTimeFormatter.ofPattern("HH:mm:ss");

    void setStatus(String msg, boolean error) {
        // Legacy cores stamp status lines; it also makes each message a distinct event.
        statusMsg.setText(" " + java.time.LocalTime.now().format(HMS) + "  " + msg);
        statusMsg.setForeground(error ? new Color(0xB0, 0x00, 0x00) : new Color(0x00, 0x40, 0x00));
    }

    private void openInquiry() {
        show(new InquiryFrame(this), 20, 20);
    }

    private void openSubAccount() {
        if (session.role() != Session.Role.TELLER || Faults.on("permission_denied")) {
            setStatus("OPERATOR NOT AUTHORIZED FOR TRAN CODE 0417", true);
            JOptionPane.showMessageDialog(this,
                    "OPERATOR " + session.operator() + " NOT AUTHORIZED FOR TRAN CODE 0417 (OPEN SUB-ACCOUNT)",
                    "Access Denied", JOptionPane.ERROR_MESSAGE);
            return;
        }
        show(new OpenSubAccountFrame(this), 60, 40);
    }

    private void show(JInternalFrame f, int x, int y) {
        desktop.add(f);
        f.setLocation(x, y);
        f.setVisible(true);
        try {
            f.setSelected(true);
        } catch (PropertyVetoException ignored) {
            // selection is cosmetic
        }
    }

    private void closeAllWindows() {
        for (JInternalFrame f : desktop.getAllFrames()) f.dispose();
    }
}
