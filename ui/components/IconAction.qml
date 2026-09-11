import QtQuick 2.15
import QtQuick.Controls 2.15

// Small square icon button used inside dense list rows.
Rectangle {
    id: root

    property string glyph: ""
    property string iconFont: ""
    property string tooltip: ""
    property bool active: true
    property color accent: "#12d6ff"
    property color panelBorder: "#12394a"
    property color panelColorDeep: "#03101a"
    property color textMuted: "#7f98aa"

    signal triggered()

    width: 26
    height: 26
    radius: 7
    color: hoverArea.containsMouse && active
        ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
        : root.panelColorDeep
    border.color: hoverArea.containsMouse && active
        ? root.accent
        : root.panelBorder
    border.width: 1
    opacity: active ? 1.0 : 0.3
    Behavior on color { ColorAnimation { duration: 100 } }

    Text {
        anchors.centerIn: parent
        text: root.glyph
        font.family: root.iconFont
        font.pixelSize: 10
        color: hoverArea.containsMouse && root.active
            ? root.accent
            : root.textMuted
    }

    ToolTip.visible: hoverArea.containsMouse && root.tooltip !== ""
    ToolTip.text: root.tooltip
    ToolTip.delay: 350

    MouseArea {
        id: hoverArea
        anchors.fill: parent
        hoverEnabled: true
        enabled: root.active
        cursorShape: Qt.PointingHandCursor
        onClicked: root.triggered()
    }
}
