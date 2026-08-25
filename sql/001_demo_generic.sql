/* The generic demo database, straight from the connector specification (section 4).
   Ordinary SQL Server tables — nothing iBase-specific here.

   The seed data follows the specification's Appendix A, and every oddity in it is
   deliberate: a person with two employers, an organisation with several staff, an
   account with two owners, a three-account transfer cycle, and text containing
   accents, commas, apostrophes and an embedded line break — because those are what
   break a bridge quietly rather than loudly.

   Run:  sqlcmd -S localhost,1433 -U sa -P "$MSSQL_SA_PASSWORD" -C -i sql/001_demo_generic.sql
*/

IF DB_ID('GraphXRConnectorDemo') IS NULL
    CREATE DATABASE GraphXRConnectorDemo;
GO
USE GraphXRConnectorDemo;
GO

/* Rebuildable from scratch: drop children before parents. */
IF OBJECT_ID('dbo.AccountTransfer','U') IS NOT NULL DROP TABLE dbo.AccountTransfer;
IF OBJECT_ID('dbo.Ownership','U')       IS NOT NULL DROP TABLE dbo.Ownership;
IF OBJECT_ID('dbo.Employment','U')      IS NOT NULL DROP TABLE dbo.Employment;
IF OBJECT_ID('dbo.Account','U')         IS NOT NULL DROP TABLE dbo.Account;
IF OBJECT_ID('dbo.Organization','U')    IS NOT NULL DROP TABLE dbo.Organization;
IF OBJECT_ID('dbo.Person','U')          IS NOT NULL DROP TABLE dbo.Person;
GO

CREATE TABLE dbo.Person (
  person_id         bigint        NOT NULL PRIMARY KEY,
  full_name         nvarchar(200) NOT NULL,
  date_of_birth     date          NULL,
  country_code      char(2)       NULL,
  risk_score        decimal(5,2)  NULL,
  source_updated_at datetime2(3)  NOT NULL
);

CREATE TABLE dbo.Organization (
  organization_id   bigint        NOT NULL PRIMARY KEY,
  name              nvarchar(250) NOT NULL,
  industry          nvarchar(100) NULL,
  country_code      char(2)       NULL,
  source_updated_at datetime2(3)  NOT NULL
);

CREATE TABLE dbo.Account (
  account_id        bigint        NOT NULL PRIMARY KEY,
  account_number    nvarchar(100) NOT NULL,
  institution       nvarchar(200) NULL,
  country_code      char(2)       NULL,
  opened_on         date          NULL,
  source_updated_at datetime2(3)  NOT NULL
);

CREATE TABLE dbo.Employment (
  employment_id     bigint        NOT NULL PRIMARY KEY,
  person_id         bigint        NOT NULL REFERENCES dbo.Person(person_id),
  organization_id   bigint        NOT NULL REFERENCES dbo.Organization(organization_id),
  job_title         nvarchar(150) NULL,
  start_date        date          NULL,
  end_date          date          NULL,
  source_updated_at datetime2(3)  NOT NULL
);

CREATE TABLE dbo.Ownership (
  ownership_id      bigint        NOT NULL PRIMARY KEY,
  person_id         bigint        NOT NULL REFERENCES dbo.Person(person_id),
  account_id        bigint        NOT NULL REFERENCES dbo.Account(account_id),
  ownership_pct     decimal(5,2)  NULL,
  source_updated_at datetime2(3)  NOT NULL
);

CREATE TABLE dbo.AccountTransfer (
  transfer_id       bigint        NOT NULL PRIMARY KEY,
  from_account_id   bigint        NOT NULL REFERENCES dbo.Account(account_id),
  to_account_id     bigint        NOT NULL REFERENCES dbo.Account(account_id),
  amount            decimal(19,4) NOT NULL,
  currency_code     char(3)       NOT NULL,
  transferred_at    datetime2(3)  NOT NULL,
  source_updated_at datetime2(3)  NOT NULL
);
GO

/* The bridge joins on these constantly. Without them every Expand is a table scan.
   Note: we add indexes to the DEMO database only, never to a real iBase database. */
CREATE INDEX IX_Employment_person      ON dbo.Employment(person_id);
CREATE INDEX IX_Employment_org         ON dbo.Employment(organization_id);
CREATE INDEX IX_Ownership_person       ON dbo.Ownership(person_id);
CREATE INDEX IX_Ownership_account      ON dbo.Ownership(account_id);
CREATE INDEX IX_Transfer_from          ON dbo.AccountTransfer(from_account_id);
CREATE INDEX IX_Transfer_to            ON dbo.AccountTransfer(to_account_id);
GO
