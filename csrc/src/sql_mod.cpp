#include <algorithm>
#include <iostream>
#include <sqlite3.h>
#include <stdexcept>
#include <string>
#include <vector>

void trim(std::string &s) {
  s.erase(0, s.find_first_not_of(' '));
  s.erase(s.find_last_not_of(' ') + 1);
}

void upper(std::string &s) {
  std::transform(s.begin(), s.end(), s.begin(), ::toupper);
}

std::vector<std::string> get_param(sqlite3 *db, const std::string &tblname,
                                   const std::string &name) {
  std::string trimmed_name = name;

  // Trim spaces and convert to upper case (mimicking Python's strip and upper)
  trim(trimmed_name);
  // upper(trimmed_name);

  std::string sql = "SELECT name, value, units, type, comments FROM " +
                    tblname + " WHERE name = ?";

  sqlite3_stmt *stmt;
  const char *tail;

  // Prepare the SQL statement
  if (sqlite3_prepare_v2(db, sql.c_str(), -1, &stmt, &tail) != SQLITE_OK) {
    throw std::runtime_error("SQL preparation failed: " +
                             std::string(sqlite3_errmsg(db)));
  }

  // Bind the parameter (the 'name' to look for)
  if (sqlite3_bind_text(stmt, 1, trimmed_name.c_str(), -1, SQLITE_STATIC) !=
      SQLITE_OK) {
    sqlite3_finalize(stmt);
    throw std::runtime_error("SQL binding failed: " +
                             std::string(sqlite3_errmsg(db)));
  }

  // Execute the query and fetch the result
  std::vector<std::string> result;
  if (sqlite3_step(stmt) == SQLITE_ROW) {
    // Collect the result into a vector (similarly to the Python list)
    result.push_back(
        reinterpret_cast<const char *>(sqlite3_column_text(stmt, 0))); // name
    result.push_back(
        reinterpret_cast<const char *>(sqlite3_column_text(stmt, 1))); // value
    result.push_back(
        reinterpret_cast<const char *>(sqlite3_column_text(stmt, 2))); // units
    result.push_back(
        reinterpret_cast<const char *>(sqlite3_column_text(stmt, 3))); // type
    result.push_back(reinterpret_cast<const char *>(
        sqlite3_column_text(stmt, 4))); // comments
  } else {
    std::cerr << "Parameter '" << trimmed_name << "' not found." << std::endl;
  }

  // Finalize the statement to release resources
  sqlite3_finalize(stmt);

  return result;
}

int get_parami(sqlite3 *db, const std::string &tblname,
               const std::string &name) {
  std::vector<std::string> res = get_param(db, tblname, name);
  return std::stoi(res[1]);
}

double get_paramd(sqlite3 *db, const std::string &tblname,
                  const std::string &name) {
  std::vector<std::string> res = get_param(db, tblname, name);
  return std::stod(res[1]);
}

std::string get_params(sqlite3 *db, const std::string &tblname,
                       const std::string &name) {
  std::vector<std::string> res = get_param(db, tblname, name);
  return res[1];
}
