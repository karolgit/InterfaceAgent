package com.corelink;

import java.awt.Color;
import java.awt.GridBagConstraints;
import java.awt.GridBagLayout;
import java.awt.Insets;
import javax.swing.JButton;
import javax.swing.JDialog;
import javax.swing.JFrame;
import javax.swing.JLabel;
import javax.swing.JPasswordField;
import javax.swing.JTextField;

/** Modal sign-on dialog. Fake operators: teller01 / viewer01, password demo123. */
final class LoginDialog extends JDialog {

    LoginDialog(JFrame owner, Session session) {
        super(owner, "CoreLink Sign On", true);
        setLayout(new GridBagLayout());
        GridBagConstraints c = new GridBagConstraints();
        c.insets = new Insets(6, 8, 6, 8);
        c.anchor = GridBagConstraints.WEST;

        JLabel userLbl = new JLabel("Operator ID:");
        JTextField user = new JTextField(14);
        userLbl.setLabelFor(user);
        JLabel passLbl = new JLabel("Password:");
        JPasswordField pass = new JPasswordField(14);
        passLbl.setLabelFor(pass);
        JLabel error = new JLabel(" ");
        error.setForeground(new Color(0xB0, 0, 0));
        error.setFont(MainFrame.MONO_BOLD);
        JButton ok = new JButton("Sign On");
        JButton cancel = new JButton("Cancel");

        c.gridx = 0; c.gridy = 0; add(userLbl, c);
        c.gridx = 1; add(user, c);
        c.gridx = 0; c.gridy = 1; add(passLbl, c);
        c.gridx = 1; add(pass, c);
        c.gridx = 0; c.gridy = 2; c.gridwidth = 2; add(error, c);
        c.gridy = 3; c.gridwidth = 1; add(ok, c);
        c.gridx = 1; add(cancel, c);

        ok.addActionListener(e -> {
            if (session.signOn(user.getText(), new String(pass.getPassword()))) {
                dispose();
            } else {
                error.setText("INVALID OPERATOR ID OR PASSWORD");
                pass.setText("");
            }
        });
        cancel.addActionListener(e -> dispose());
        getRootPane().setDefaultButton(ok);
        pack();
        setLocationRelativeTo(owner);
    }
}
