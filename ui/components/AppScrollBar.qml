import QtQuick 2.15
import QtQuick.Controls 2.15

ScrollBar {
    id: control
    property color accent: "#12d6ff"

    implicitWidth: orientation === Qt.Vertical ? 8 : (parent ? parent.width : 100)
    implicitHeight: orientation === Qt.Horizontal ? 8 : (parent ? parent.height : 100)
    padding: 2
    minimumSize: 0.08

    contentItem: Rectangle {
        implicitWidth: 4
        implicitHeight: 4
        radius: 2
        color: control.pressed
            ? control.accent
            : Qt.rgba(control.accent.r, control.accent.g, control.accent.b,
                      control.hovered ? 0.55 : 0.28)
        Behavior on color { ColorAnimation { duration: 120 } }
    }

    background: Rectangle {
        color: "transparent"
    }
}
