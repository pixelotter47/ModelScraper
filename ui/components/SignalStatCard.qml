import QtQuick 2.15
import QtQuick.Layouts 1.15

Item {
    id: root
    property string label: ""
    property string value: ""
    property string hint: ""
    property string icon: ""
    property string iconFontFamily: ""
    property color accent: "#12d6ff"
    property color surface: "#061521"
    property color borderColor: "#12394a"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property string headingFont: "Bahnschrift"
    property string bodyFont: "Bahnschrift"

    implicitWidth: 170
    implicitHeight: 64

    Rectangle {
        anchors.fill: parent
        radius: 9
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.lighter(root.surface, 1.16) }
            GradientStop { position: 1.0; color: Qt.darker(root.surface, 1.12) }
        }
        border.color: root.borderColor
        border.width: 1
    }

    Rectangle {
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        width: 2
        height: parent.height - 20
        radius: 1
        color: root.accent
        opacity: 0.8
    }

    RowLayout {
        anchors.fill: parent
        anchors.margins: 12
        anchors.leftMargin: 14
        spacing: 11

        Rectangle {
            visible: root.icon !== ""
            Layout.preferredWidth: 32
            Layout.preferredHeight: 32
            radius: 8
            color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.12)
            border.color: Qt.rgba(root.accent.r, root.accent.g, root.accent.b, 0.25)
            border.width: 1

            Text {
                anchors.centerIn: parent
                text: root.icon
                font.family: root.iconFontFamily
                font.pixelSize: 13
                color: root.accent
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 1

            Text {
                Layout.fillWidth: true
                text: root.label
                font.family: root.bodyFont
                font.pixelSize: 10
                font.letterSpacing: 1.2
                color: root.textMuted
                elide: Text.ElideRight
            }

            Text {
                Layout.fillWidth: true
                text: root.value
                font.family: root.headingFont
                font.pixelSize: 17
                font.letterSpacing: 0.3
                color: root.textPrimary
                elide: Text.ElideRight
            }

            Text {
                Layout.fillWidth: true
                visible: root.hint !== ""
                text: root.hint
                font.family: root.bodyFont
                font.pixelSize: 10
                color: root.textMuted
                opacity: 0.85
                elide: Text.ElideRight
            }
        }
    }
}
