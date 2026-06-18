// 分支同步面板 — 原生 macOS 窗口（WKWebView 加载本地面板服务）
// 编译: swiftc -O main.swift -o launcher
import Cocoa
import WebKit

let PORT = 8799
let PANEL_URL = URL(string: "http://127.0.0.1:\(PORT)/sync")!

func serverUp() -> Bool {
    var ok = false
    let sem = DispatchSemaphore(value: 0)
    var req = URLRequest(url: PANEL_URL)
    req.timeoutInterval = 0.6
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        if let r = resp as? HTTPURLResponse, r.statusCode == 200 { ok = true }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 1.0)
    return ok
}

class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    var server: Process?
    var attempts = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        if !serverUp() { startServer() }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 980, height: 720),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "分支同步面板"
        window.center()
        window.setFrameAutosaveName("SyncBranchesPanel")

        webView = WKWebView(frame: window.contentView!.bounds,
                            configuration: WKWebViewConfiguration())
        webView.autoresizingMask = [.width, .height]
        webView.uiDelegate = self
        window.contentView!.addSubview(webView)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        tryLoad()
    }

    func startServer() {
        guard let res = Bundle.main.resourcePath else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [res + "/sync-branches-ui.py", String(PORT)]
        var env = ProcessInfo.processInfo.environment
        env["SYNC_NO_BROWSER"] = "1"
        if env["SYNC_DEFAULT_BASE"] == nil { env["SYNC_DEFAULT_BASE"] = NSHomeDirectory() }
        p.environment = env
        do { try p.run(); server = p } catch {
            showError("启动面板服务失败：\(error.localizedDescription)\n请确认系统已安装 python3 和 git。")
        }
    }

    func tryLoad() {
        if serverUp() {
            webView.load(URLRequest(url: PANEL_URL))
            return
        }
        attempts += 1
        if attempts > 30 {
            showError("面板服务启动超时。\n请确认系统已安装 python3 和 git（日志: /tmp/sync-branches-ui.log）。")
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { [weak self] in self?.tryLoad() }
    }

    func showError(_ msg: String) {
        let a = NSAlert()
        a.messageText = "分支同步面板"
        a.informativeText = msg
        a.alertStyle = .warning
        a.addButton(withTitle: "好")
        a.runModal()
    }

    // 让页面里的 confirm()/alert() 走原生弹窗
    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = "分支同步面板"
        a.informativeText = message
        a.addButton(withTitle: "确认")
        a.addButton(withTitle: "取消")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        showError(message)
        completionHandler()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) {
        server?.terminate()   // 只回收自己拉起的服务；复用的外部服务不受影响
    }
}

// 主菜单：没有它 Cmd+C/V/X/A 等快捷键不会分发到输入框
func buildMainMenu() -> NSMenu {
    let main = NSMenu()

    let appItem = NSMenuItem()
    main.addItem(appItem)
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: "隐藏分支同步面板", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    appMenu.addItem(NSMenuItem.separator())
    appMenu.addItem(withTitle: "退出分支同步面板", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu

    let editItem = NSMenuItem()
    main.addItem(editItem)
    let edit = NSMenu(title: "编辑")
    edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
    edit.addItem(NSMenuItem.separator())
    edit.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    editItem.submenu = edit

    let winItem = NSMenuItem()
    main.addItem(winItem)
    let win = NSMenu(title: "窗口")
    win.addItem(withTitle: "关闭窗口", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
    win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
    winItem.submenu = win

    return main
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.mainMenu = buildMainMenu()
app.setActivationPolicy(.regular)
app.run()
