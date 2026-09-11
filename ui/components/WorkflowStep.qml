import QtQuick 2.15

// One numbered step row: circular index chip, action button, optional hint below.
Item {
    id: root
    property string stepNumber: "1"
    property string text: ""
    property string hint: ""
    property string icon: ""
    property string iconFontFamily: ""
    property bool enabled: true
    property color accent: "#12d6ff"
    property color surface: "#071622"
    property color surfaceHover: "#0b2535"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property string headingFont: "Bahnschrift"
    property string bodyFont: "Bahnschrift"
    property bool showConnector: true
    property bool hovered: false
    signal clicked()

    implicitHeight: 40 + (hint !== "" ? hintText.implicitHeight + 3 : 0)

    // Connector line down to the next step
    Rectangle {
        visible: root.showConnector
        width: 1
        anchors.top: chip.bottom
        anchors.topMargin: 2
        anchors.bottom: parent.bottom
        anchors.bottomMargin: -10
        anchors.horizontalCenter: chip.horizontalCenter
        color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.22)
    }

    Rectangle {
        id: chip
        width: 26
        height: 26
        radius: 13
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.topMargin: 7
        color: root.hovered && root.enabled
            ? Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.16)
            : "transparent"
        border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b,
                              root.enabled ? 0.75 : 0.3)
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }

        Text {
            anchors.centerIn: parent
            text: root.stepNumber
            font.family: root.headingFont
            font.pixelSize: 12
            color: root.enabled ? root.accent : root.textMuted
        }
    }

    AppButton {
        id: actionButton
        anchors.left: chip.right
        anchors.leftMargin: 10
        anchors.right: parent.right
        anchors.top: parent.top
        height: 40
        text: root.text
        icon: root.icon
        iconFontFamily: root.iconFontFamily
        accent: root.accent
        baseColor: root.surface
        hoverColor: root.surfaceHover
        textColor: root.textPrimary
        fontFamily: root.headingFont
        enabled: root.enabled
        onHoveredChanged: root.hovered = hovered
        onClicked: root.clicked()
    }

    Text {
        id: hintText
        visible: root.hint !== ""
        anchors.left: chip.right
        anchors.leftMargin: 12
        anchors.right: parent.right
        anchors.top: actionButton.bottom
        anchors.topMargin: 3
        text: root.hint
        font.family: root.bodyFont
        font.pixelSize: 11
        color: root.textMuted
        opacity: 0.85
        wrapMode: Text.WordWrap
    }
}
