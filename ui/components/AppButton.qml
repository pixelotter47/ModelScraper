import QtQuick 2.15

Item {
    id: root
    property string text: ""
    property string icon: ""
    property string iconFontFamily: ""
    property bool enabled: true
    property color accent: "#12d6ff"
    property color baseColor: "#071622"
    property color hoverColor: "#0b2535"
    property color textColor: "#eef8ff"
    property string fontFamily: "Bahnschrift"
    property bool hovered: false
    property bool pressed: false
    property bool compact: false
    // "primary" = filled accent, "secondary" = outlined surface, "ghost" = borderless
    property string kind: "secondary"
    signal clicked()

    readonly property bool isPrimary: kind === "primary"
    readonly property bool isGhost: kind === "ghost"

    implicitWidth: 200
    implicitHeight: compact ? 34 : 42
    opacity: enabled ? 1.0 : 0.38

    // Soft outer glow, only while hovering an enabled button
    Rectangle {
        anchors.fill: parent
        anchors.margins: -2
        radius: 9
        color: root.accent
        opacity: root.enabled && root.hovered ? (root.isPrimary ? 0.22 : 0.10) : 0.0
        visible: !root.isGhost
        Behavior on opacity { NumberAnimation { duration: 140 } }
    }

    Rectangle {
        id: bg
        anchors.fill: parent
        radius: 7
        color: root.isPrimary
            ? (root.pressed
                ? Qt.darker(root.accent, 1.25)
                : (root.hovered ? Qt.lighter(root.accent, 1.10) : root.accent))
            : (root.pressed
                ? Qt.darker(root.baseColor, 1.15)
                : (root.hovered ? root.hoverColor : root.baseColor))
        border.color: root.isPrimary
            ? Qt.rgba(1, 1, 1, root.hovered ? 0.35 : 0.16)
            : (root.isGhost
                ? Qt.rgba(1, 1, 1, root.hovered ? 0.22 : 0.10)
                : Qt.rgba(root.accent.r, root.accent.g, root.accent.b,
                          root.hovered ? 0.95 : 0.42))
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
    }

    Row {
        anchors.centerIn: parent
        width: Math.min(implicitWidth, parent.width - 24)
        spacing: 8

        Text {
            visible: root.icon !== ""
            text: root.icon
            font.family: root.iconFontFamily
            font.pixelSize: root.compact ? 11 : 12
            color: root.isPrimary ? "#04121c" : root.accent
            opacity: root.isPrimary ? 0.9 : 0.95
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            width: Math.min(implicitWidth,
                            root.width - 24 - (root.icon !== "" ? 20 : 0))
            text: root.text
            font.family: root.fontFamily
            font.pixelSize: root.compact ? 12 : 13
            font.letterSpacing: 0.3
            color: root.isPrimary ? "#04121c" : root.textColor
            elide: Text.ElideRight
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.enabled
        cursorShape: Qt.PointingHandCursor
        onEntered: root.hovered = true
        onExited: { root.hovered = false; root.pressed = false }
        onPressed: root.pressed = true
        onReleased: root.pressed = false
        onClicked: root.clicked()
    }
}
