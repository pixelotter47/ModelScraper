import QtQuick 2.15

// Compact numeric stepper: [-] value [+]
Row {
    id: root

    property int value: 0
    property int minimum: 0
    property int maximum: 99
    property int step: 1
    property color accent: "#12d6ff"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property color panelBorder: "#12394a"
    property string headingFont: "Bahnschrift"
    property string suffix: ""

    signal changed(int value)

    spacing: 6
    height: 34

    function apply(next) {
        var clamped = Math.max(root.minimum, Math.min(root.maximum, next))
        if (clamped !== root.value) {
            root.changed(clamped)
        }
    }

    Rectangle {
        id: minusButton
        anchors.verticalCenter: parent.verticalCenter
        width: 30
        height: 30
        radius: 7
        readonly property bool active: root.value > root.minimum
        color: minusArea.containsMouse && active ? "#0b3445" : "#03101a"
        border.color: minusArea.containsMouse && active
            ? root.accent
            : root.panelBorder
        border.width: 1
        opacity: active ? 1.0 : 0.35

        Text {
            anchors.centerIn: parent
            text: "−"
            font.family: root.headingFont
            font.pixelSize: 16
            color: minusArea.containsMouse && minusButton.active
                ? root.accent
                : root.textMuted
        }

        MouseArea {
            id: minusArea
            anchors.fill: parent
            hoverEnabled: true
            enabled: minusButton.active
            cursorShape: Qt.PointingHandCursor
            onClicked: root.apply(root.value - root.step)
        }
    }

    Text {
        anchors.verticalCenter: parent.verticalCenter
        width: 52
        horizontalAlignment: Text.AlignHCenter
        text: root.value + (root.suffix !== "" ? " " + root.suffix : "")
        font.family: root.headingFont
        font.pixelSize: 14
        color: root.textPrimary
    }

    Rectangle {
        id: plusButton
        anchors.verticalCenter: parent.verticalCenter
        width: 30
        height: 30
        radius: 7
        readonly property bool active: root.value < root.maximum
        color: plusArea.containsMouse && active ? "#0b3445" : "#03101a"
        border.color: plusArea.containsMouse && active
            ? root.accent
            : root.panelBorder
        border.width: 1
        opacity: active ? 1.0 : 0.35

        Text {
            anchors.centerIn: parent
            text: "+"
            font.family: root.headingFont
            font.pixelSize: 16
            color: plusArea.containsMouse && plusButton.active
                ? root.accent
                : root.textMuted
        }

        MouseArea {
            id: plusArea
            anchors.fill: parent
            hoverEnabled: true
            enabled: plusButton.active
            cursorShape: Qt.PointingHandCursor
            onClicked: root.apply(root.value + root.step)
        }
    }
}
