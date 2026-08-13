# NetworkUSB Architecture & MenuBar Standards

## 1. MenuBar UI Standards (NetworkUSBMenu.swift)
- **Language**: All UI strings, menu items, headers, and logs MUST be 100% in English.
- **Emoji-Free Invariant**: Do NOT use emojis (`ℹ️`, `🔄`, `🛍`, `🖥`, `📍`, etc.) in menu titles, headers, status messages, or alerts. Keep the interface clean and natively styled.
- **Server Formatting**: Display servers as pure `host:port` strings (e.g. `127.0.0.1:8721`). Do NOT append descriptive names (e.g. `Local Tart VM`) or enclose host:port in parentheses.
- **Header Naming**: Use `Servers:` as the main section header. Do NOT use uppercase `SERVERS:`, `MAГАЗИНЫ`, or pushpin icons (`📍`).

## 2. Automation & User Flow Constraints
- **100% Automatic Operation**: Do NOT include manual input dialogs (e.g. `Add Server (IP / Host)…`) or manual store connector items when servers are auto-discovered.
- **No Interruptive Alerts**: Do NOT present modal `NSAlert` popup windows upon completing network or bridge scans. All discovery updates must occur silently within the menu bar UI.
- **Redundancy Invariant**: Do NOT display redundant status summary lines (e.g. `Bridge: 1` or `(No servers discovered)`) beneath the `Servers:` section.

## 3. macOS Cocoa Architecture (NSStatusItem)
- **NSMenuDelegate Invariant**: Always construct `NSStatusItem.menu` ONCE with `NSMenuDelegate` (`menuNeedsUpdate(_ menu: NSMenu)`).
- **Never Reassign `statusItem.menu` inside Timers**: Recreating or reassigning `statusItem.menu` dynamically inside background timer loops breaks macOS event tracking and closes open dropdown menus.
