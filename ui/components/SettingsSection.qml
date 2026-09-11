import QtQuick 2.15
import QtQuick.Layouts 1.15

// A Panel that grows to fit a vertical stack of setting rows.
Item {
    id: root

    property string title: ""
    property string titleIcon: ""
    property string titleIconFont: ""
    property string titleFont: "Bahnschrift"
    property color titleColor: "#eef8ff"
    property color titleAccent: "#12d6ff"
    property color panelColor: "#061521"
    property color borderColor: "#0f3b4f"
    property int spacing: 14
    property int padding: 18

    default property alias sectionContent: column.data
    property alias headerExtra: panel.headerExtra

    Layout.fillWidth: true
    implicitHeight: column.implicitHeight
        + padding * 2
        + (title !== "" ? 32 : 0)

    Panel {
        id: panel
        anchors.fill: parent
        padding: root.padding
        panelColor: root.panelColor
        borderColor: root.borderColor
        accentColor: root.titleAccent
        title: root.title
        titleIcon: root.titleIcon
        titleIconFont: root.titleIconFont
        titleFont: root.titleFont
        titleColor: root.titleColor
        titleAccent: root.titleAccent

        ColumnLayout {
            id: column
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            spacing: root.spacing
        }
    }
}
