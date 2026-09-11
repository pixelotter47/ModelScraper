import QtQuick 2.15

Item {
    id: root
    property string text: ""
    property string icon: ""
    property string iconFontFamily: ""
    property bool active: false
    property bool enabled: true
    property color accent: "#12d6ff"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#8aa1b2"
    property string fontFamily: "Bahnschrift"
    property bool hovered: false
    signal clicked()

    implicitHeight: 40
    implicitWidth: 180
    opacity: enabled ? 1 : 0.4

    Rectangle {
        anchors.fill: parent
        radius: 7
        color: root.active
            ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.13)
            : (root.hovered ? "#0a2436" : "transparent")
        border.color: root.active
            ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.65)
            : "transparent"
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
    }

    Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: 2
        height: parent.height - 14
        radius: 1
        color: root.accent
        opacity: root.active ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 120 } }
    }

    Row {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
        anchors.leftMargin: 13
        anchors.rightMargin: 10
        spacing: 10

        Item {
            width: 16
            height: 16
            anchors.verticalCenter: parent.verticalCenter
            Text {
                visible: root.icon !== ""
                anchors.centerIn: parent
                text: root.icon
                font.family: root.iconFontFamily
                font.pixelSize: 12
                color: root.active ? root.accent : root.textMuted
            }
        }

        Text {
            width: parent.width - 26
            text: root.text
            font.family: root.fontFamily
            font.pixelSize: 13
            font.letterSpacing: 0.3
            color: root.active ? root.textPrimary : (root.hovered ? "#c9dcea" : root.textMuted)
            elide: Text.ElideRight
            anchors.verticalCenter: parent.verticalCenter
        }
    }

    MouseArea {
        anchors.fill: parent
        enabled: root.enabled
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onEntered: root.hovered = true
        onExited: root.hovered = false
        onClicked: root.clicked()
    }
}
