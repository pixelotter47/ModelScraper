import QtQuick 2.15
import QtQuick.Layouts 1.15

// Label + hint on the left, a single control on the right.
Item {
    id: root

    property string label: ""
    property string hint: ""
    property string labelFont: "Bahnschrift"
    property string hintFont: "Bahnschrift"
    property color labelColor: "#eef8ff"
    property color hintColor: "#7f98aa"
    property color dangerColor: "#ff4d3d"
    property string error: ""
    property int controlWidth: 200
    property int controlHeight: 34

    default property alias control: controlHost.data

    Layout.fillWidth: true
    implicitHeight: Math.max(labelColumn.implicitHeight, root.controlHeight)

    RowLayout {
        anchors.fill: parent
        spacing: 18

        ColumnLayout {
            id: labelColumn
            Layout.fillWidth: true
            Layout.alignment: Qt.AlignVCenter
            spacing: 2

            Text {
                Layout.fillWidth: true
                text: root.label
                font.family: root.labelFont
                font.pixelSize: 13
                color: root.labelColor
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                visible: root.hint !== "" || root.error !== ""
                text: root.error !== "" ? root.error : root.hint
                font.family: root.hintFont
                font.pixelSize: 11
                color: root.error !== "" ? root.dangerColor : root.hintColor
                wrapMode: Text.WordWrap
                opacity: root.error !== "" ? 1.0 : 0.9
            }
        }

        Item {
            id: controlHost
            Layout.preferredWidth: root.controlWidth
            Layout.preferredHeight: root.controlHeight
            Layout.alignment: Qt.AlignVCenter
        }
    }
}
