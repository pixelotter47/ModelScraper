import QtQuick 2.15
import QtQuick.Controls 2.15

// Framed single-line input matching the dashboard search fields.
Rectangle {
    id: root

    property alias text: input.text
    property alias placeholder: input.placeholderText
    property alias readOnly: input.readOnly
    property alias echoMode: input.echoMode
    property color accent: "#12d6ff"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property string bodyFont: "Bahnschrift"
    property string icon: ""
    property string iconFont: ""

    signal accepted()
    signal edited()

    implicitHeight: 34
    implicitWidth: 200
    radius: 8
    color: "#03101a"
    border.color: input.activeFocus
        ? Qt.rgba(accent.r, accent.g, accent.b, 0.7)
        : Qt.rgba(1, 1, 1, 0.08)
    border.width: 1
    Behavior on border.color { ColorAnimation { duration: 120 } }

    Text {
        id: iconLabel
        visible: root.icon !== ""
        anchors.left: parent.left
        anchors.leftMargin: 10
        anchors.verticalCenter: parent.verticalCenter
        text: root.icon
        font.family: root.iconFont
        font.pixelSize: 11
        color: root.textMuted
    }

    TextField {
        id: input
        anchors.fill: parent
        anchors.leftMargin: iconLabel.visible ? 30 : 10
        anchors.rightMargin: 10
        verticalAlignment: TextInput.AlignVCenter
        font.family: root.bodyFont
        font.pixelSize: 13
        color: root.textPrimary
        placeholderTextColor: root.textMuted
        background: null
        selectByMouse: true
        onAccepted: root.accepted()
        onEditingFinished: root.edited()
    }
}
