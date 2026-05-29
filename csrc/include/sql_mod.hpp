#ifndef SQL_MOD
#define SQL_MOD

#include<string>
#include<vector>
#include<sqlite3.h>

std::vector<std::string> get_param(sqlite3* db, const std::string& tblname, const std::string& name);
int get_parami(sqlite3* db, const std::string& tblname, const std::string& name);
double get_paramd(sqlite3* db, const std::string& tblname, const std::string& name);
std::string get_params(sqlite3* db, const std::string& tblname, const std::string& name);
#endif