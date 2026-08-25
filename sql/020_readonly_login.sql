/* A login that can read and nothing else.

   The specification is firm on this (its section 10 and acceptance test AC-09):
   the bridge connects with SELECT permission only. iBase schema changes go through
   iBase Designer, never through SQL.

   The bridge ALSO refuses write keywords on its own, before a statement reaches the
   driver. That is not redundant: it turns a future bug in the query generator into a
   loud crash rather than a write, and it lets the user see "this bridge only reads"
   instead of a SQL Server permission error.

   Usage — set a real password first:
     sqlcmd -S localhost,1433 -U sa -P "$MSSQL_SA_PASSWORD" -C \
       -v pwd="<a strong password>" db="GraphXRConnectorDemo" -i sql/020_readonly_login.sql
*/
:setvar pwd "CHANGE_ME"
:setvar db  "GraphXRConnectorDemo"
GO
USE master;
GO
IF SUSER_ID('ibase_ro') IS NULL
    CREATE LOGIN ibase_ro WITH PASSWORD = '$(pwd)', CHECK_POLICY = ON;
GO
USE [$(db)];
GO
IF USER_ID('ibase_ro') IS NULL
    CREATE USER ibase_ro FOR LOGIN ibase_ro;
GO
ALTER ROLE db_datareader ADD MEMBER ibase_ro;
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE TO ibase_ro;
GO
/* Prove it. This must report that the write was refused. */
EXECUTE AS USER = 'ibase_ro';
BEGIN TRY
    EXEC('UPDATE dbo.Person SET country_code = country_code WHERE 1 = 0');
    SELECT 'PROBLEM: the read-only login was able to UPDATE' AS result;
END TRY
BEGIN CATCH
    SELECT 'OK: writes are refused for ibase_ro' AS result;
END CATCH
REVERT;
GO
