package com.cua.bridge;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.awt.AWTEvent;
import java.awt.Color;
import java.awt.Component;
import java.awt.Dialog;
import java.awt.EventQueue;
import java.awt.Font;
import java.awt.Frame;
import java.awt.Graphics2D;
import java.awt.KeyboardFocusManager;
import java.awt.Point;
import java.awt.Rectangle;
import java.awt.Toolkit;
import java.awt.Window;
import java.awt.event.InputEvent;
import java.awt.event.KeyEvent;
import java.awt.event.MouseEvent;
import java.awt.event.WindowEvent;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.lang.instrument.Instrumentation;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import javax.accessibility.Accessible;
import javax.accessibility.AccessibleAction;
import javax.accessibility.AccessibleComponent;
import javax.accessibility.AccessibleContext;
import javax.accessibility.AccessibleEditableText;
import javax.accessibility.AccessibleRelation;
import javax.accessibility.AccessibleRole;
import javax.accessibility.AccessibleState;
import javax.accessibility.AccessibleStateSet;
import javax.imageio.ImageIO;
import javax.swing.JComboBox;
import javax.swing.JLabel;
import javax.swing.JPasswordField;
import javax.swing.JTable;
import javax.swing.JWindow;
import javax.swing.SwingUtilities;
import javax.swing.text.JTextComponent;

/**
 * Surface bridge for Java desktop apps, loaded with -javaagent into an UNMODIFIED app.
 *
 * Perception reads the Java Accessibility API, the same data the Windows Java Access Bridge
 * exposes to screen readers: roles, names, states, bounds, text. A few values (combo
 * selection, table cells, text contents) are read from the Swing component for simplicity;
 * the Access Bridge exposes equivalents through AccessibleSelection, AccessibleTable and
 * AccessibleText. Actions use accessibility actions where possible, so they work without
 * moving the real mouse and while the desktop is locked.
 *
 * It also owns the control seam for human handoff: who holds the session, an input lock
 * while automation is in control, and a recorder for what the human did.
 */
public final class BridgeAgent {
    private static final String INDICATOR_NAME = "__cua_control_indicator__";

    private static volatile String mode = "free"; // free | agent | human | paused
    private static volatile String holder = "";
    private static final ThreadLocal<Boolean> SYNTHETIC = ThreadLocal.withInitial(() -> false);
    private static final List<Map<String, Object>> EVENTS = new ArrayList<>();
    private static final Map<String, Accessible> REFS = new HashMap<>();
    private static JWindow indicator;
    private static JLabel indicatorLabel;

    private BridgeAgent() {}

    public static void premain(String args, Instrumentation inst) {
        int port = 8740;
        if (args != null) {
            for (String kv : args.split(",")) {
                if (kv.startsWith("port=")) port = Integer.parseInt(kv.substring(5));
            }
        }
        final int p = port;
        EventQueue.invokeLater(BridgeAgent::installHooks);
        Thread t = new Thread(() -> startServer(p), "cua-bridge-http");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------------ hooks

    private static void installHooks() {
        Toolkit tk = Toolkit.getDefaultToolkit();
        tk.getSystemEventQueue().push(new GuardedQueue());
        tk.addAWTEventListener(BridgeAgent::record,
                AWTEvent.MOUSE_EVENT_MASK | AWTEvent.KEY_EVENT_MASK | AWTEvent.WINDOW_EVENT_MASK);
    }

    /** Drops real human input while automation holds the session. Synthetic agent input bypasses the queue. */
    private static final class GuardedQueue extends EventQueue {
        @Override
        protected void dispatchEvent(AWTEvent e) {
            if ("agent".equals(mode) && isHumanInput(e)) {
                if (e.getID() == MouseEvent.MOUSE_PRESSED || e.getID() == KeyEvent.KEY_PRESSED) {
                    addEvent(Map.of("kind", "blocked_input", "actor", "human",
                            "detail", "input ignored: automation in control"));
                }
                return;
            }
            super.dispatchEvent(e);
        }

        private static boolean isHumanInput(AWTEvent e) {
            if (e instanceof KeyEvent) return true;
            if (e instanceof MouseEvent me) {
                if (me.getComponent() != null && INDICATOR_NAME.equals(windowName(me.getComponent()))) return false;
                int id = me.getID();
                return id == MouseEvent.MOUSE_PRESSED || id == MouseEvent.MOUSE_RELEASED
                        || id == MouseEvent.MOUSE_CLICKED || id == MouseEvent.MOUSE_DRAGGED
                        || id == MouseEvent.MOUSE_WHEEL;
            }
            return false;
        }
    }

    private static String windowName(Component c) {
        Window w = c instanceof Window win ? win : SwingUtilities.getWindowAncestor(c);
        return w == null ? null : w.getName();
    }

    private static final StringBuilder typing = new StringBuilder();
    private static Component typingTarget;

    private static void record(AWTEvent e) {
        boolean synthetic = SYNTHETIC.get();
        String actor = synthetic ? "agent" : "human";
        if (e instanceof WindowEvent we) {
            if (INDICATOR_NAME.equals(we.getWindow().getName())) return;
            if (we.getID() == WindowEvent.WINDOW_OPENED || we.getID() == WindowEvent.WINDOW_CLOSED) {
                flushTyping();
                addEvent(Map.of("kind", we.getID() == WindowEvent.WINDOW_OPENED ? "window_opened" : "window_closed",
                        "window", title(we.getWindow())));
            }
            return;
        }
        if (synthetic) return; // agent actions are logged by the controller, not here
        if (!"human".equals(mode) && !"free".equals(mode) && !"paused".equals(mode)) return;
        if (e instanceof MouseEvent me && me.getID() == MouseEvent.MOUSE_CLICKED) {
            flushTyping();
            Map<String, Object> ev = new LinkedHashMap<>();
            ev.put("kind", "click");
            ev.put("actor", actor);
            ev.put("target", describe(me.getComponent()));
            ev.put("x", me.getXOnScreen());
            ev.put("y", me.getYOnScreen());
            addEvent(ev);
        } else if (e instanceof KeyEvent ke) {
            if (ke.getID() == KeyEvent.KEY_TYPED && !Character.isISOControl(ke.getKeyChar())) {
                if (typingTarget != ke.getComponent()) flushTyping();
                typingTarget = ke.getComponent();
                typing.append(ke.getKeyChar());
            } else if (ke.getID() == KeyEvent.KEY_PRESSED && isSpecial(ke.getKeyCode())) {
                flushTyping();
                addEvent(Map.of("kind", "key", "actor", actor, "key", KeyEvent.getKeyText(ke.getKeyCode()),
                        "target", describe(ke.getComponent())));
            }
        }
    }

    private static boolean isSpecial(int code) {
        return code == KeyEvent.VK_ENTER || code == KeyEvent.VK_ESCAPE || code == KeyEvent.VK_TAB
                || (code >= KeyEvent.VK_F1 && code <= KeyEvent.VK_F12);
    }

    private static void flushTyping() {
        if (typing.length() == 0 || typingTarget == null) return;
        Map<String, Object> ev = new LinkedHashMap<>();
        ev.put("kind", "type");
        ev.put("actor", "human");
        ev.put("target", describe(typingTarget));
        boolean secret = typingTarget instanceof JPasswordField;
        ev.put("text", secret ? "[SECRET]" : typing.toString()); // Python redacts PII before persisting
        ev.put("length", typing.length());
        addEvent(ev);
        typing.setLength(0);
        typingTarget = null;
    }

    private static Map<String, Object> describe(Component c) {
        Map<String, Object> m = new LinkedHashMap<>();
        if (c == null) return m;
        AccessibleContext ac = c.getAccessibleContext();
        if (ac != null) {
            m.put("role", ac.getAccessibleRole().toDisplayString(Locale.US));
            m.put("name", clean(ac.getAccessibleName()));
        }
        Window w = SwingUtilities.getWindowAncestor(c);
        m.put("window", w == null ? "" : title(w));
        return m;
    }

    private static synchronized void addEvent(Map<String, Object> ev) {
        Map<String, Object> copy = new LinkedHashMap<>(ev);
        copy.put("seq", EVENTS.size());
        copy.put("ts", System.currentTimeMillis());
        copy.put("mode", mode);
        EVENTS.add(copy);
    }

    // ------------------------------------------------------------------ server

    private static void startServer(int port) {
        try {
            HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", port), 0);
            server.createContext("/health", ex -> send(ex, 200, Map.of("ok", true, "mode", mode, "holder", holder)));
            server.createContext("/tree", ex -> handle(ex, BridgeAgent::tree));
            server.createContext("/screenshot", BridgeAgent::screenshot);
            server.createContext("/act", ex -> handle(ex, BridgeAgent::act));
            server.createContext("/events", ex -> handle(ex, BridgeAgent::events));
            server.createContext("/control", ex -> handle(ex, BridgeAgent::control));
            server.start();
            System.err.println("[cua-bridge] listening on 127.0.0.1:" + port);
        } catch (IOException e) {
            System.err.println("[cua-bridge] failed to start: " + e);
        }
    }

    private interface Handler {
        Object apply(Map<String, Object> body, Map<String, String> query) throws Exception;
    }

    @SuppressWarnings("unchecked")
    private static void handle(HttpExchange ex, Handler h) throws IOException {
        try {
            String raw = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            Map<String, Object> body = raw.isBlank() ? Map.of() : (Map<String, Object>) Json.parse(raw);
            send(ex, 200, h.apply(body, query(ex)));
        } catch (IllegalArgumentException e) {
            send(ex, 400, Map.of("error", String.valueOf(e.getMessage())));
        } catch (Exception e) {
            send(ex, 500, Map.of("error", e.toString()));
        }
    }

    private static Map<String, String> query(HttpExchange ex) {
        Map<String, String> q = new HashMap<>();
        String s = ex.getRequestURI().getRawQuery();
        if (s != null) {
            for (String kv : s.split("&")) {
                int i = kv.indexOf('=');
                if (i > 0) q.put(kv.substring(0, i), java.net.URLDecoder.decode(kv.substring(i + 1), StandardCharsets.UTF_8));
            }
        }
        return q;
    }

    private static void send(HttpExchange ex, int code, Object body) throws IOException {
        byte[] b = Json.write(body).getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(code, b.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(b);
        }
    }

    private static <T> T onEdt(java.util.concurrent.Callable<T> c) throws Exception {
        if (SwingUtilities.isEventDispatchThread()) return c.call();
        AtomicReference<T> out = new AtomicReference<>();
        AtomicReference<Exception> err = new AtomicReference<>();
        SwingUtilities.invokeAndWait(() -> {
            try {
                out.set(c.call());
            } catch (Exception e) {
                err.set(e);
            }
        });
        if (err.get() != null) throw err.get();
        return out.get();
    }

    // ------------------------------------------------------------------ perception

    private static List<Window> showingWindows() {
        List<Window> out = new ArrayList<>();
        for (Window w : Window.getWindows()) {
            if (w.isShowing() && !INDICATOR_NAME.equals(w.getName())) out.add(w);
        }
        return out;
    }

    private static String title(Window w) {
        if (w instanceof Frame f) return f.getTitle();
        if (w instanceof Dialog d) return d.getTitle();
        return "";
    }

    private static Object tree(Map<String, Object> body, Map<String, String> q) throws Exception {
        return onEdt(() -> {
            REFS.clear();
            List<Object> windows = new ArrayList<>();
            List<Window> ws = showingWindows();
            for (int wi = 0; wi < ws.size(); wi++) {
                Window w = ws.get(wi);
                Map<String, Object> wm = new LinkedHashMap<>();
                wm.put("index", wi);
                wm.put("title", title(w));
                wm.put("kind", w instanceof Dialog ? "dialog" : w instanceof Frame ? "frame" : "window");
                wm.put("modal", w instanceof Dialog d && d.isModal());
                wm.put("bounds", rect(w.getBounds()));
                List<Object> nodes = new ArrayList<>();
                walk(w, "w" + wi, 0, null, title(w), nodes, false);
                wm.put("nodes", nodes);
                windows.add(wm);
            }
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("app", "java-swing");
            out.put("control", Map.of("mode", mode, "holder", holder));
            out.put("windows", windows);
            return out;
        });
    }

    private static void walk(Accessible a, String ref, int depth, String container, String window,
                             List<Object> out, boolean inMenu) {
        AccessibleContext ac = a.getAccessibleContext();
        if (ac == null || depth > 40) return;
        AccessibleStateSet ss = ac.getAccessibleStateSet();
        boolean showing = ss.contains(AccessibleState.SHOWING) || depth == 0;
        if (!showing && !inMenu) return;
        AccessibleRole role = ac.getAccessibleRole();
        String roleName = role.toDisplayString(Locale.US);
        String name = clean(ac.getAccessibleName());
        if (role == AccessibleRole.INTERNAL_FRAME) container = name;

        Map<String, Object> n = new LinkedHashMap<>();
        n.put("ref", ref);
        n.put("role", roleName);
        n.put("name", name);
        n.put("depth", depth);
        n.put("window", window);
        if (container != null) n.put("container", container);
        List<String> states = new ArrayList<>();
        for (AccessibleState s : ss.toArray()) states.add(s.toDisplayString(Locale.US));
        n.put("states", states);
        if (!showing) n.put("hidden_menu_item", true);
        AccessibleComponent comp = ac.getAccessibleComponent();
        if (comp != null && showing) {
            try {
                Point p = comp.getLocationOnScreen();
                if (p != null) n.put("bounds", List.of(p.x, p.y, comp.getSize().width, comp.getSize().height));
            } catch (Exception ignored) {
                // component not on screen
            }
        }
        AccessibleRelation rel = ac.getAccessibleRelationSet().get(AccessibleRelation.LABELED_BY);
        if (rel != null && rel.getTarget().length > 0 && rel.getTarget()[0] instanceof Accessible la) {
            n.put("labeled_by", clean(la.getAccessibleContext().getAccessibleName()));
        }
        AccessibleAction act = ac.getAccessibleAction();
        if (act != null && act.getAccessibleActionCount() > 0) n.put("actionable", true);
        if (ac.getAccessibleEditableText() != null) n.put("editable_text", true);

        Object self = a;
        boolean recurse = true;
        if (self instanceof JPasswordField) {
            n.put("value", ((JPasswordField) self).getPassword().length > 0 ? "********" : "");
            recurse = false;
        } else if (self instanceof JTextComponent tc) {
            n.put("value", tc.getText());
            recurse = false;
        } else if (self instanceof JComboBox<?> cb) {
            Object sel = cb.getSelectedItem();
            n.put("value", sel == null ? "" : sel.toString());
            List<String> opts = new ArrayList<>();
            for (int i = 0; i < cb.getItemCount(); i++) opts.add(String.valueOf(cb.getItemAt(i)));
            n.put("options", opts);
            recurse = false;
        } else if (self instanceof JTable t) {
            List<String> cols = new ArrayList<>();
            for (int c = 0; c < t.getColumnCount(); c++) cols.add(t.getColumnName(c));
            List<Object> rows = new ArrayList<>();
            for (int r = 0; r < t.getRowCount(); r++) {
                List<String> row = new ArrayList<>();
                for (int c = 0; c < t.getColumnCount(); c++) row.add(String.valueOf(t.getValueAt(r, c)));
                rows.add(row);
            }
            n.put("columns", cols);
            n.put("rows", rows);
            recurse = false;
        }
        REFS.put(ref, a);
        out.add(n);
        if (!recurse) return;
        int count = ac.getAccessibleChildrenCount();
        boolean childInMenu = inMenu || role == AccessibleRole.MENU || role == AccessibleRole.MENU_BAR;
        for (int i = 0; i < count; i++) {
            Accessible child = ac.getAccessibleChild(i);
            if (child != null) walk(child, ref + "." + i, depth + 1, container, window, out, childInMenu);
        }
    }

    private static String clean(String s) {
        if (s == null) return "";
        if (s.regionMatches(true, 0, "<html>", 0, 6)) {
            s = s.replaceAll("(?i)<br\\s*/?>", "\n").replaceAll("<[^>]+>", "");
        }
        return s.trim();
    }

    private static List<Integer> rect(Rectangle r) {
        return List.of(r.x, r.y, r.width, r.height);
    }

    private static void screenshot(HttpExchange ex) throws IOException {
        try {
            byte[] png = onEdt(() -> {
                List<Window> ws = showingWindows();
                if (ws.isEmpty()) throw new IllegalStateException("no windows showing");
                Rectangle all = new Rectangle(ws.get(0).getBounds());
                for (Window w : ws) all = all.union(w.getBounds());
                BufferedImage img = new BufferedImage(all.width, all.height, BufferedImage.TYPE_INT_RGB);
                Graphics2D g = img.createGraphics();
                g.setColor(new Color(0x20, 0x20, 0x20));
                g.fillRect(0, 0, all.width, all.height);
                for (Window w : ws) {
                    Rectangle b = w.getBounds();
                    Graphics2D wg = (Graphics2D) g.create(b.x - all.x, b.y - all.y, b.width, b.height);
                    wg.setColor(new Color(0xE0, 0xE0, 0xE0));
                    wg.fillRect(0, 0, b.width, b.height);
                    w.printAll(wg);
                    int top = w.getInsets().top;
                    wg.setColor(new Color(0x3A, 0x3A, 0x3A));
                    wg.fillRect(0, 0, b.width, top);
                    wg.setColor(Color.WHITE);
                    wg.setFont(new Font(Font.SANS_SERIF, Font.PLAIN, 12));
                    wg.drawString(title(w), 8, Math.max(12, top - 9));
                    wg.dispose();
                }
                g.dispose();
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                ImageIO.write(img, "png", bos);
                ex.getResponseHeaders().set("X-Origin", all.x + "," + all.y);
                return bos.toByteArray();
            });
            ex.getResponseHeaders().set("Content-Type", "image/png");
            ex.sendResponseHeaders(200, png.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(png);
            }
        } catch (Exception e) {
            send(ex, 500, Map.of("error", e.toString()));
        }
    }

    // ------------------------------------------------------------------ actions

    private static Accessible resolve(Map<String, Object> body) {
        String ref = String.valueOf(body.get("ref"));
        Accessible a = REFS.get(ref);
        if (a == null) throw new IllegalArgumentException("unknown ref " + ref + " (observe again)");
        return a;
    }

    private static Object act(Map<String, Object> body, Map<String, String> q) throws Exception {
        String op = String.valueOf(body.get("op"));
        switch (op) {
            case "click" -> {
                Accessible a = resolve(body);
                SwingUtilities.invokeLater(() -> {
                    AccessibleContext ac = a.getAccessibleContext();
                    AccessibleAction act = ac.getAccessibleAction();
                    if (act != null && act.getAccessibleActionCount() > 0) {
                        act.doAccessibleAction(0);
                    } else if (ac.getAccessibleComponent() != null) {
                        ac.getAccessibleComponent().requestFocus();
                    }
                });
            }
            case "set_text" -> {
                Accessible a = resolve(body);
                String text = String.valueOf(body.getOrDefault("text", ""));
                onEdt(() -> {
                    AccessibleContext ac = a.getAccessibleContext();
                    AccessibleEditableText et = ac.getAccessibleEditableText();
                    if (et == null) throw new IllegalArgumentException("target is not editable text");
                    if (ac.getAccessibleComponent() != null) ac.getAccessibleComponent().requestFocus();
                    et.setTextContents(text);
                    return null;
                });
            }
            case "select" -> {
                Accessible a = resolve(body);
                String option = String.valueOf(body.get("option"));
                onEdt(() -> {
                    if (!(a instanceof JComboBox<?> cb)) throw new IllegalArgumentException("target is not a combo box");
                    for (int i = 0; i < cb.getItemCount(); i++) {
                        String s = String.valueOf(cb.getItemAt(i));
                        if (s.equals(option) || s.startsWith(option)) {
                            cb.setSelectedIndex(i);
                            return null;
                        }
                    }
                    throw new IllegalArgumentException("option not found: " + option);
                });
            }
            case "key" -> {
                String key = String.valueOf(body.get("key")).toUpperCase(Locale.US);
                int code = keyCode(key);
                SwingUtilities.invokeLater(() -> synthetic(() -> {
                    Component target = keyTarget();
                    long now = System.currentTimeMillis();
                    target.dispatchEvent(new KeyEvent(target, KeyEvent.KEY_PRESSED, now, 0, code, KeyEvent.CHAR_UNDEFINED));
                    target.dispatchEvent(new KeyEvent(target, KeyEvent.KEY_RELEASED, now, 0, code, KeyEvent.CHAR_UNDEFINED));
                }));
            }
            case "click_at" -> {
                int x = ((Number) body.get("x")).intValue();
                int y = ((Number) body.get("y")).intValue();
                boolean human = Boolean.TRUE.equals(body.get("as_human"));
                SwingUtilities.invokeLater(() -> {
                    Runnable r = () -> clickAt(x, y);
                    if (human) r.run(); else synthetic(r);
                });
            }
            case "close" -> {
                // Housekeeping verb every surface has: close an internal frame or a window.
                Accessible a = resolve(body);
                SwingUtilities.invokeLater(() -> {
                    if (a instanceof javax.swing.JInternalFrame f) {
                        try {
                            f.setClosed(true);
                        } catch (java.beans.PropertyVetoException ignored) {
                            // frame refused to close
                        }
                    } else if (a instanceof Window w) {
                        w.dispatchEvent(new WindowEvent(w, WindowEvent.WINDOW_CLOSING));
                    }
                });
            }
            case "human_type" -> {
                // Operator simulator only: types real KEY_TYPED events so the recorder sees them as human input.
                Accessible a = resolve(body);
                String text = String.valueOf(body.getOrDefault("text", ""));
                SwingUtilities.invokeLater(() -> {
                    Component c = (Component) a;
                    c.requestFocusInWindow();
                    for (char ch : text.toCharArray()) {
                        c.dispatchEvent(new KeyEvent(c, KeyEvent.KEY_TYPED, System.currentTimeMillis(), 0,
                                KeyEvent.VK_UNDEFINED, ch));
                    }
                });
            }
            case "human_click" -> {
                Accessible a = resolve(body);
                SwingUtilities.invokeLater(() -> {
                    Component c = (Component) a;
                    Point p = c.getLocationOnScreen();
                    clickAt(p.x + c.getWidth() / 2, p.y + c.getHeight() / 2);
                });
            }
            default -> throw new IllegalArgumentException("unknown op " + op);
        }
        return Map.of("ok", true, "op", op);
    }

    private static void synthetic(Runnable r) {
        SYNTHETIC.set(true);
        try {
            r.run();
        } finally {
            SYNTHETIC.set(false);
        }
    }

    private static int keyCode(String key) {
        return switch (key) {
            case "ENTER", "RETURN" -> KeyEvent.VK_ENTER;
            case "ESCAPE", "ESC" -> KeyEvent.VK_ESCAPE;
            case "TAB" -> KeyEvent.VK_TAB;
            case "SPACE" -> KeyEvent.VK_SPACE;
            default -> {
                if (key.matches("F([1-9]|1[0-2])")) yield KeyEvent.VK_F1 + Integer.parseInt(key.substring(1)) - 1;
                throw new IllegalArgumentException("unsupported key " + key);
            }
        };
    }

    /** Top-most modal dialog if any, else the focused component, else the main frame's root pane. */
    private static Component keyTarget() {
        List<Window> ws = showingWindows();
        Window top = null;
        for (Window w : ws) if (w instanceof Dialog d && d.isModal()) top = w;
        if (top == null) {
            Component f = KeyboardFocusManager.getCurrentKeyboardFocusManager().getFocusOwner();
            if (f != null) return f;
            top = ws.isEmpty() ? null : ws.get(0);
        }
        if (top == null) throw new IllegalStateException("no window");
        Component f = top.getFocusOwner();
        if (f != null) return f;
        if (top instanceof javax.swing.RootPaneContainer rpc) return rpc.getRootPane();
        return top;
    }

    private static void clickAt(int x, int y) {
        List<Window> ws = showingWindows();
        Window hit = null;
        for (Window w : ws) if (w.getBounds().contains(x, y)) hit = w; // later windows are on top
        if (hit == null) return;
        Point local = new Point(x - hit.getLocationOnScreen().x, y - hit.getLocationOnScreen().y);
        Component c = SwingUtilities.getDeepestComponentAt(hit, local.x, local.y);
        if (c == null) return;
        Point cp = SwingUtilities.convertPoint(hit, local, c);
        long now = System.currentTimeMillis();
        int mods = InputEvent.BUTTON1_DOWN_MASK;
        c.dispatchEvent(new MouseEvent(c, MouseEvent.MOUSE_PRESSED, now, mods, cp.x, cp.y, x, y, 1, false, MouseEvent.BUTTON1));
        c.dispatchEvent(new MouseEvent(c, MouseEvent.MOUSE_RELEASED, now, 0, cp.x, cp.y, x, y, 1, false, MouseEvent.BUTTON1));
        c.dispatchEvent(new MouseEvent(c, MouseEvent.MOUSE_CLICKED, now, 0, cp.x, cp.y, x, y, 1, false, MouseEvent.BUTTON1));
    }

    // ------------------------------------------------------------------ control + events

    private static Object control(Map<String, Object> body, Map<String, String> q) throws Exception {
        if (body.containsKey("mode")) {
            String m = String.valueOf(body.get("mode"));
            if (!List.of("free", "agent", "human", "paused").contains(m)) throw new IllegalArgumentException("bad mode " + m);
            String prev = mode;
            mode = m;
            holder = String.valueOf(body.getOrDefault("holder", ""));
            addEvent(Map.of("kind", "control_change", "from", prev, "to", m, "holder", holder));
            SwingUtilities.invokeLater(BridgeAgent::updateIndicator);
        }
        return Map.of("mode", mode, "holder", holder);
    }

    private static void updateIndicator() {
        if (indicator == null) {
            indicator = new JWindow();
            indicator.setName(INDICATOR_NAME);
            indicator.setFocusableWindowState(false);
            indicator.setAlwaysOnTop(true);
            indicatorLabel = new JLabel();
            indicatorLabel.setOpaque(true);
            indicatorLabel.setFont(new Font(Font.SANS_SERIF, Font.BOLD, 12));
            indicatorLabel.setForeground(Color.WHITE);
            indicatorLabel.setBorder(javax.swing.BorderFactory.createEmptyBorder(4, 10, 4, 10));
            indicator.add(indicatorLabel);
        }
        switch (mode) {
            case "agent" -> { indicatorLabel.setText("AUTOMATION IN CONTROL - keyboard/mouse locked"); indicatorLabel.setBackground(new Color(0x8B, 0x00, 0x00)); }
            case "human" -> { indicatorLabel.setText("OPERATOR IN CONTROL (" + holder + ") - actions recorded"); indicatorLabel.setBackground(new Color(0x00, 0x60, 0x20)); }
            case "paused" -> { indicatorLabel.setText("AUTOMATION PAUSED - awaiting operator"); indicatorLabel.setBackground(new Color(0xB0, 0x70, 0x00)); }
            default -> { indicator.setVisible(false); return; }
        }
        indicator.pack();
        List<Window> ws = showingWindows();
        if (!ws.isEmpty()) {
            Rectangle b = ws.get(0).getBounds();
            indicator.setLocation(b.x + b.width - indicator.getWidth() - 12, b.y + 4);
        }
        indicator.setVisible(true);
    }

    private static synchronized Object events(Map<String, Object> body, Map<String, String> q) {
        EventQueue.invokeLater(BridgeAgent::flushTyping);
        int since = Integer.parseInt(q.getOrDefault("since", "0"));
        List<Object> out = new ArrayList<>();
        for (int i = Math.max(0, since); i < EVENTS.size(); i++) out.add(EVENTS.get(i));
        return Map.of("events", out, "next", EVENTS.size());
    }
}
