"""Build gcode.hda — a self-contained G-code importer SOP.

The file is read by a Python SOP inside the asset (standard library only); placing,
colouring, trimming and the bead geometry are VEX wrangles and stock nodes, so the
asset needs nothing outside the .hda file.

Run:  hython build.py
"""
import glob
import os
import re

import hou

HERE = os.path.dirname(os.path.abspath(__file__))
HDA_PATH = os.path.join(HERE, "gcode.hda")
NAME = "gcode::import::1.0"
SUBMENU = "Import"
DETAIL, PRIMS, POINTS = 0, 1, 2

# Tab menu entry; hython does not generate one for new assets.
TOOL_SHELF = """\
<?xml version="1.0" encoding="UTF-8"?>
<shelfDocument>
  <tool name="$HDA_DEFAULT_TOOL" label="$HDA_LABEL" icon="$HDA_ICON">
    <toolMenuContext name="viewer">
      <contextNetType>SOP</contextNetType>
    </toolMenuContext>
    <toolMenuContext name="network">
      <contextOpType>$HDA_TABLE_AND_NAME</contextOpType>
    </toolMenuContext>
    <toolSubmenu>%s</toolSubmenu>
    <script scriptType="python"><![CDATA[import soptoolutils

soptoolutils.genericTool(kwargs, '$HDA_NAME')]]></script>
  </tool>
</shelfDocument>
"""

HELP = """\
= G-code Import =

#type: node
#context: sop
#internal: gcode::import
#icon: SOP/file
#tags: gcode, 3d print, fdm, toolpath, import

\"\"\"Читает G-code 3D-принтера: траектории сопла с атрибутами для рендера.\"\"\"

Понимает Bambu Studio, Orca, PrusaSlicer, Cura, Simplify3D: линии G0/G1, дуги G2/G3
(в том числе с подъёмом по Z и полными витками P), абсолютные и относительные
координаты и экструзию, G92, дюймы. Метки слайсера (тип линии, слой, толщина слоя,
объект) берутся из комментариев.

Каждый непрерывный участок пути одного типа становится отдельной кривой: валик внешней
стенки, кусок заполнения, перемещение. Значения, которые меняются по ходу движения
(ширина, скорость, время), лежат на точках.

Ширина валика считается по объёму выдавленного пластика с учётом flow ratio и
толщины слоя — так же, как её считает слайсер. Поэтому видно и Arachne с переменной
шириной, и сужение на scarf seam.

@inputs

Нет входов: геометрия берётся из файла.

@outputs

Geometry:
    Кривые или трубки валиков (см. Geometry).

Nozzle:
    Одна точка: где сопло находится в момент Progress (с учётом холостых перемещений).
    Удобно для Copy to Points с моделью печатающей головки.

@parameters

G-code File:
    #id: file
    Файл `.gcode`. Чтение занимает пару секунд на часовую печать; меняются
    остальные параметры без перечитывания.

Reload:
    #id: reload
    Перечитать файл (если его пересохранили под тем же именем).

Geometry:
    #id: geometry
    Curves — полилинии по оси сопла (точки на кончике сопла, то есть на верхней
    грани валика). Лёгкие, рендерятся как кривые: атрибут `width` — диаметр кривой
    для Karma.
    Tubes — настоящие валики: сечение как у слайсера (прямоугольник с полукруглыми
    боками), ширина валика, высота слоя, верх на уровне сопла. Концы круглые в плане,
    как пятно сопла. Плавные повороты идут одним валиком со скосом, на острых (больше
    60° или слишком тесных для ширины валика) валик разрезается на куски с круглыми
    концами, которые перекрываются, как след сопла на развороте: так трубка нигде не
    выворачивается. Нормали посчитаны точно.

Round End Divisions:
    #id: divisions
    Сколько отрезков на каждом полукруглом боку сечения. 2 — шестигранник,
    3–4 — хорошо для крупных планов.

Polygon Soups:
    #id: polysoup
    Собрать каждый валик в polygon soup: та же геометрия для рендера, в разы
    меньше памяти. Выключить, если дальше нужны обычные полигоны.

Travel Moves:
    #id: travel
    Добавить холостые перемещения и wipe (группа `travel`). В режиме Tubes они
    остаются кривыми.

Start/End G-code:
    #id: custom
    Добавить стартовый и финальный код принтера: продувочную линию, калибровки
    (группа `custom`). По умолчанию только сама модель.

Arc Tolerance:
    #id: arctol
    Насколько дуги G2/G3 могут отходить от ломаной, в миллиметрах.

Y Up:
    #id: yup
    G-code пишется с осью Z вверх. Включено — повернуть как Houdini (Y вверх),
    так же как Transform с Rotate X = -90.

Origin:
    #id: origin
    G-code — координаты принтера (угол стола в нуле).
    Bed Centre — центр стола в нуле (размер стола из настроек слайсера).
    Print Centre — центр модели по горизонтали в нуле. Стол всегда на высоте 0.

Scale:
    #id: scale
    1 — единица сцены равна миллиметру. 0.001 — метры.

Colour:
    #id: colour
    Filament — цвет пластика из настроек слайсера (`filament_colour`), для
    нескольких экструдеров — по номеру инструмента.
    Feature — цвета превью слайсера по типу линии: стенки, заполнение, мосты...

Progress:
    #id: progress
    Сколько напечатано, от 0 до 1 по времени печати. Анимируй его, чтобы модель
    печаталась: последний валик обрывается точно в позиции сопла, без скачков.

== Атрибуты ==

Точки:

`width`:
    Ширина валика в единицах сцены.

`height`:
    Толщина валика (высота слоя). У мостов Bambu она больше слоя: мост круглый.

`time`:
    Секунды от начала печати. Подогнано под оценку времени самого слайсера (метки
    M73 или TIME_ELAPSED), поэтому темп совпадает с настоящей печатью.

`speed`:
    Скорость сопла, мм/с.

Примитивы:

`feature`:
    Тип линии, как его назвал слайсер: `Outer wall`, `Inner wall`, `Sparse infill`,
    `Bridge`... У перемещений — `Travel` или `Wipe`.

`layer`:
    Номер слоя с нуля.

`object`:
    Объект на столе (имя или номер от слайсера), если их несколько.

`tool`:
    Номер экструдера/филамента (для AMS — номер слота).

`Cd`:
    Цвет (см. Colour).

Группы примитивов `travel` и `custom`.

Detail: `print_time` (секунды), `layer_count`, `filament_colour`, `nozzle_diameter`,
`slicer`, `source`.

== Приёмы ==

* Только внешний вид: Blast по `@feature!="Outer wall"` оставит наружные стенки —
  в разы меньше геометрии для дальних планов.
* «Горячий» след за соплом: в шейдере или wrangle `@time` против текущего
  времени (`ch("progress") * detail(..., "print_time")`).
* Печать за 10 секунд при 24 к/с: Progress = `$F / 240`.
"""


def source(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


def hide_when(template, condition):
    template.setConditional(hou.parmCondType.HideWhen, condition)
    return template


def wrangle(parent, name, run_over, code, input0=None, input1=None):
    node = parent.createNode("attribwrangle", name)
    node.parm("class").set(run_over)
    node.parm("snippet").set(source(os.path.join("vex", code + ".vfl")))
    if input0 is not None:
        node.setInput(0, input0)
    if input1 is not None:
        node.setInput(1, input1)
    return node


def blast(parent, name, source_node, group, keep):
    node = parent.createNode("blast", name)
    node.setInput(0, source_node)
    node.parm("group").set(group)
    node.parm("grouptype").set("prims")
    node.parm("negate").set(keep)
    return node


def switcher(parent, name, expression, *inputs):
    node = parent.createNode("switch", name)
    for index, source_node in enumerate(inputs):
        node.setInput(index, source_node)
    node.parm("input").setExpression(expression)
    return node


def parms():
    return [
        hou.StringParmTemplate(
            "file", "G-code File", 1, string_type=hou.stringParmType.FileReference,
            file_type=hou.fileType.Any, join_with_next=True,
            tags={"filechooser_pattern": "*.gcode *.gco *.g"},
            help="Файл G-code. Читается один раз, остальные параметры его не перечитывают."),
        hou.ButtonParmTemplate(
            "reload", "Reload", script_callback="kwargs['node'].node('parse').cook(force=True)",
            script_callback_language=hou.scriptLanguage.Python,
            help="Перечитать файл."),
        hou.SeparatorParmTemplate("sep1"),
        hou.MenuParmTemplate(
            "geometry", "Geometry", ("curves", "tubes"), ("Curves", "Tubes"), default_value=1,
            help="Curves — оси валиков. Tubes — валики с сечением как у слайсера."),
        hide_when(hou.IntParmTemplate(
            "divisions", "Round End Divisions", 1, default_value=(3,), min=2, max=8,
            min_is_strict=True, help="Отрезков на каждом полукруглом боку сечения."),
            "{ geometry != tubes }"),
        hide_when(hou.ToggleParmTemplate(
            "polysoup", "Polygon Soups", default_value=True,
            help="Каждый валик — polygon soup: меньше памяти, та же картинка."),
            "{ geometry != tubes }"),
        hou.ToggleParmTemplate(
            "travel", "Travel Moves", default_value=False,
            help="Добавить холостые перемещения и wipe (группа travel)."),
        hou.ToggleParmTemplate(
            "custom", "Start/End G-code", default_value=False,
            help="Добавить стартовый и финальный код: продувку, калибровки (группа custom)."),
        hou.FloatParmTemplate(
            "arctol", "Arc Tolerance", 1, default_value=(0.01,), min=0.001, max=0.1,
            min_is_strict=True, help="Допуск дуг G2/G3, мм."),
        hou.SeparatorParmTemplate("sep2"),
        hou.ToggleParmTemplate(
            "yup", "Y Up", default_value=True,
            help="Повернуть из Z-вверх принтера в Y-вверх Houdini (как Rotate X = -90)."),
        hou.MenuParmTemplate(
            "origin", "Origin", ("gcode", "bed", "print"),
            ("G-code (Bed Corner)", "Bed Centre", "Print Centre"), default_value=2,
            help="Что поставить в ноль по горизонтали. Стол всегда на высоте 0."),
        hou.FloatParmTemplate(
            "scale", "Scale", 1, default_value=(1.0,), min=0.001, max=10.0,
            help="1 — единица сцены = миллиметр, 0.001 — метры."),
        hou.SeparatorParmTemplate("sep3"),
        hou.MenuParmTemplate(
            "colour", "Colour", ("filament", "feature"), ("Filament", "Feature"),
            default_value=0,
            help="Цвет пластика из слайсера или цвета превью по типу линии."),
        hou.SeparatorParmTemplate("sep4"),
        hou.FloatParmTemplate(
            "progress", "Progress", 1, default_value=(1.0,), min=0.0, max=1.0,
            min_is_strict=True, max_is_strict=True,
            help="Доля напечатанного по времени печати. Анимируй от 0 до 1."),
    ]


def make_asset(geo):
    sub = geo.createNode("subnet", "gcode_import")

    parse = sub.createNode("python", "parse")
    parse.parm("python").set(source("parse.py"))
    place = wrangle(sub, "place", POINTS, "place", parse)
    colour = wrangle(sub, "colour", PRIMS, "colour", place)
    select = wrangle(sub, "select", PRIMS, "select", colour)
    reveal = wrangle(sub, "reveal", PRIMS, "reveal", select)

    # Tubes: Sweep makes the mesh and carries the attributes, "shape" places every point.
    beads = blast(sub, "beads", reveal, "travel", keep=False)
    split = wrangle(sub, "split", PRIMS, "split", beads)
    sweep = sub.createNode("sweep::2.0", "sweep")
    sweep.setInput(0, split)
    sweep.parm("surfaceshape").set("tube")
    sweep.parm("cols").setExpression('2 * ch("../divisions") + 2')
    sweep.parm("endcaptype").set("grid")
    sweep.parm("capdivs").set(3)                 # round ends: two rings and a pole
    sweep.parm("triangularpoles").set(1)
    sweep.parm("attribsfrombackbone").set("*")
    for toggle in ("addptrow", "addptcol", "addcurvenum"):
        sweep.parm(toggle).set(1)
    sweep.parm("primtype").setExpression('if(ch("../polysoup"), 5, 1)')  # polysoup / poly
    shape = wrangle(sub, "shape", POINTS, "shape", sweep, split)
    sweep_attribs = sub.createNode("attribdelete", "sweep_attribs")
    sweep_attribs.setInput(0, shape)
    sweep_attribs.parm("ptdel").set("ptrow ptcol curvenum")
    # Travel moves stay curves next to the tubes.
    moves = blast(sub, "moves", reveal, "travel", keep=True)
    with_moves = sub.createNode("merge", "with_moves")
    with_moves.setInput(0, sweep_attribs)
    with_moves.setInput(1, moves)
    tubes = switcher(sub, "tubes", 'ch("../travel")', sweep_attribs, with_moves)

    geometry = switcher(sub, "curves_or_tubes", 'ch("../geometry")', reveal, tubes)
    clean = sub.createNode("attribdelete", "clean")
    clean.setInput(0, geometry)
    clean.parm("dtldel").set("print_centre bed_centre")

    nozzle = wrangle(sub, "nozzle", DETAIL, "nozzle", None, colour)

    for index, node in enumerate((clean, nozzle)):
        out = sub.createNode("output", "output%d" % index)
        out.parm("outputidx").set(index)
        out.setInput(0, node)
        if index == 0:
            out.setDisplayFlag(True)
            out.setRenderFlag(True)
    sub.layoutChildren()

    asset = sub.createDigitalAsset(
        name=NAME, hda_file_name=HDA_PATH, description="G-code Import",
        min_num_inputs=0, max_num_inputs=0, create_backup=False)
    definition = asset.type().definition()
    definition.setMaxNumOutputs(2)

    group = hou.ParmTemplateGroup()
    for template in parms():
        group.append(template)
    definition.setParmTemplateGroup(group)

    # Connector labels live only in the DialogScript.
    labels = '    outputlabel\t1\t"Geometry"\n    outputlabel\t2\t"Nozzle"\n'
    script = definition.sections()["DialogScript"].contents()
    if re.search(r"^[ \t]*(?:input|output)label\b", script, flags=re.M):
        script = re.sub(r"(?:^[ \t]*(?:input|output)label\b.*\n)+", lambda m: labels, script,
                        count=1, flags=re.M)
    else:
        script = re.sub(r"(^[ \t]*help\s*\{)", labels.replace("\\", "\\\\") + r"\1", script,
                        count=1, flags=re.M)
    definition.addSection("DialogScript", script)
    definition.setIcon("SOP_file")
    definition.addSection("Help", HELP)
    definition.addSection("Tools.shelf", TOOL_SHELF % SUBMENU)
    definition.addSection("MessageNodes", "parse")  # show the reader's warnings on the asset

    asset.matchCurrentDefinition()
    return asset


def main():
    if os.path.exists(HDA_PATH):
        os.remove(HDA_PATH)
    geo = hou.node("/obj").createNode("geo", "build")
    make_asset(geo)

    # Houdini backs the file up on every definition edit; the build is reproducible, drop them.
    backup = os.path.join(HERE, "backup")
    for name in glob.glob(os.path.join(backup, "gcode_bak*.hda")):
        os.remove(name)
    if os.path.isdir(backup) and not os.listdir(backup):
        os.rmdir(backup)

    for definition in hou.hda.definitionsInFile(HDA_PATH):
        print("built", definition.nodeTypeName(), "-", definition.description())


if __name__ == "__main__":
    main()
