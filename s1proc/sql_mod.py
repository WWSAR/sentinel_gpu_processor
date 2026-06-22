import sqlite3


# create a table
def add_tbl(c, tblname):
    # create a table
    #    tblname = tblname.strip().upper()
    tblname = tblname.strip()
    try:
        c.execute(
            "create table "
            + tblname
            + " (t1key INTEGER PRIMARY KEY,\n\
        name TEXT unique,value TEXT,units text,type text,comments text);"
        )
    except sqlite3.Error:
        pass


def rm_tbl(c, tblname):
    # remove a table
    tblname = tblname.strip()
    try:
        c.execute("drop table " + tblname)
    except sqlite3.Error:
        print("Warning: Table " + tblname + " does not exist")
        pass


def add_param(c, tblname, pname):
    # add a parameter to the table
    name = pname.strip()  # trim spaces, upper case

    try:
        c.execute("insert into " + tblname + " (name) values ('" + name + "')")
    except Exception as e:
        print(e)
        print("Error: Cannot add " + name + " to Table " + tblname)
        pass


def del_param(c, tblname, name):
    # add a parameter to the table
    name = name.strip()  # trim spaces, upper case
    c.execute("delete from " + tblname + " where name='" + name + "'")


def edit_param(c, tblname, name, value, units, type, comment):
    # add a parameter to the table
    name = name.strip()  # trim spaces, upper case
    value = str(value).strip()
    units = units.strip()
    type = type.strip()
    comment = comment.strip()
    try:
        c.execute(
            "update "
            + tblname
            + " set value='"
            + value
            + "',units='"
            + units
            + "',type='"
            + type
            + "',comments='"
            + comment
            + "' where name='"
            + name
            + "'"
        )
    except sqlite3.Error as e:
        print("In function EDIT_PARAM: " + name + " does not exist", e)


def get_param(c, tblname, name):
    # get values from the table (returned as a list)
    # output = [name,value,units,type,comment]
    name = name.strip()  # trim spaces, upper case
    try:
        c.execute(
            "select name,value,units,type,comments from "
            + tblname
            + " where name='"
            + name
            + "'"
        )
        row = c.fetchone()
        return [row[0], row[1], row[2], row[3], row[4]]
    except (sqlite3.Error, TypeError) as e:
        print("Parameter '" + name + "' not found", e)


def valuef(c, tblname, name):
    # return float value of a parameter in database table
    name = name.strip()  # trim spaces, upper case
    try:
        c.execute(
            "select name,value,units,type,comments from "
            + tblname
            + " where name='"
            + name
            + "'"
        )
        row = c.fetchone()
        return float(row[1])
    except (sqlite3.Error, TypeError, ValueError) as e:
        print("Parameter '" + name + "' not found", e)


def valuei(c, tblname, name):
    # return int value of a parameter in database table
    name = name.strip()  # trim spaces, upper case
    try:
        c.execute(
            "select name,value,units,type,comments from "
            + tblname
            + " where name='"
            + name
            + "'"
        )
        row = c.fetchone()
        return int(row[1])
    except (sqlite3.Error, TypeError, ValueError) as e:
        print("Parameter '" + name + "' not found", e)


def valuec(c, tblname, name):
    # return string value of a parameter in database table
    name = name.strip()  # trim spaces, upper case
    try:
        c.execute(
            "select name,value,units,type,comments from "
            + tblname
            + " where name='"
            + name
            + "'"
        )
        row = c.fetchone()
        return row[1]
    except (sqlite3.Error, TypeError) as e:
        print("Parameter '" + name + "' not found", e)
