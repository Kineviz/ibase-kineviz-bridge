/* An iBase-SHAPED demo database.

   This is NOT a copy of a real i2 iBase schema — we do not have one, and i2 does
   not publish the physical column names (the pages the specification cites are
   behind a customer login). What it does is reproduce the *conventions* that are
   publicly documented, so the adapter can be built and tested honestly:

     - one table per entity type, one table per link type
     - record ids are strings with a three-letter type prefix (PER0000123)
     - endpoints reachable two ways: columns on the link table, and the _LinkEnd
       system table
     - a link type that is POLYMORPHIC — Associate joins Person-Person,
       Person-Organisation and Organisation-Vehicle
     - underscore-prefixed system tables, including _AL_ (audit) and _FTS_
       (full-text) families that the bridge must refuse to touch

   Treat every column name here as a placeholder. Against a real database, run
   `discover` and let it read the actual names off the server.

   Run:  sqlcmd -S localhost,1433 -U sa -P "$MSSQL_SA_PASSWORD" -C -i sql/010_demo_ibase.sql
*/

IF DB_ID('IBaseShapedDemo') IS NULL
    CREATE DATABASE IBaseShapedDemo;
GO
USE IBaseShapedDemo;
GO

IF OBJECT_ID('dbo._LinkEnd','U')     IS NOT NULL DROP TABLE dbo._LinkEnd;
IF OBJECT_ID('dbo.Involved_In','U')  IS NOT NULL DROP TABLE dbo.Involved_In;
IF OBJECT_ID('dbo.Associate','U')    IS NOT NULL DROP TABLE dbo.Associate;
IF OBJECT_ID('dbo.Event','U')        IS NOT NULL DROP TABLE dbo.Event;
IF OBJECT_ID('dbo.Vehicle','U')      IS NOT NULL DROP TABLE dbo.Vehicle;
IF OBJECT_ID('dbo.Organisation','U') IS NOT NULL DROP TABLE dbo.Organisation;
IF OBJECT_ID('dbo.Person','U')       IS NOT NULL DROP TABLE dbo.Person;
IF OBJECT_ID('dbo._AL_Audit','U')    IS NOT NULL DROP TABLE dbo._AL_Audit;
IF OBJECT_ID('dbo._FTS_Index','U')   IS NOT NULL DROP TABLE dbo._FTS_Index;
GO

/* ---- entity tables: one per entity type, string record ids ---- */
CREATE TABLE dbo.Person (
  Person_ID    nvarchar(20)  NOT NULL PRIMARY KEY,   -- 'PER0000123'
  Surname      nvarchar(100) NOT NULL,
  Forename     nvarchar(100) NULL,
  DateOfBirth  date          NULL,
  Nationality  nvarchar(50)  NULL
);
CREATE TABLE dbo.Organisation (
  Organisation_ID nvarchar(20)  NOT NULL PRIMARY KEY, -- 'ORG0000045'
  Name            nvarchar(200) NOT NULL,
  Sector          nvarchar(100) NULL
);
CREATE TABLE dbo.Vehicle (
  Vehicle_ID   nvarchar(20)  NOT NULL PRIMARY KEY,    -- 'VEH0000007'
  Registration nvarchar(20)  NOT NULL,
  Make         nvarchar(50)  NULL,
  Model        nvarchar(50)  NULL
);
CREATE TABLE dbo.[Event] (
  Event_ID     nvarchar(20)  NOT NULL PRIMARY KEY,    -- 'EVT0000900'
  Description  nvarchar(400) NULL,
  OccurredOn   datetime2(3)  NULL
);

/* ---- link tables: one per link type ----
   Associate keeps its endpoints in Link1/Link2 and is polymorphic: which entity
   table a record id belongs to is told by its prefix. */
CREATE TABLE dbo.Associate (
  Associate_ID nvarchar(20)  NOT NULL PRIMARY KEY,    -- 'ASS0000001'
  Link1        nvarchar(20)  NOT NULL,
  Link2        nvarchar(20)  NOT NULL,
  Confidence   nvarchar(20)  NULL,
  FirstSeen    date          NULL,
  LastSeen     date          NULL
);

/* Involved_In keeps its endpoints in the _LinkEnd system table instead. */
CREATE TABLE dbo.Involved_In (
  Involved_In_ID nvarchar(20) NOT NULL PRIMARY KEY,   -- 'INV0000001'
  Role           nvarchar(80) NULL,
  RecordedOn     date         NULL
);

/* The endpoint table. One row per end of a link: End = 1 is the source, 2 the target. */
CREATE TABLE dbo._LinkEnd (
  LinkTable nvarchar(64) NOT NULL,
  LinkId    nvarchar(20) NOT NULL,
  [End]     tinyint      NOT NULL,
  RecordId  nvarchar(20) NOT NULL,
  CONSTRAINT PK__LinkEnd PRIMARY KEY (LinkTable, LinkId, [End])
);

/* System tables the bridge must never map. Present only so we can prove it skips them. */
CREATE TABLE dbo._AL_Audit  (AuditId bigint IDENTITY PRIMARY KEY, [Action] nvarchar(50), [User] nvarchar(50));
CREATE TABLE dbo._FTS_Index (FtsId bigint IDENTITY PRIMARY KEY, RecordId nvarchar(20), Term nvarchar(200));
GO

/* IBM publishes a support note about _LinkEnd needing the right indexes; without
   them every traversal scans it. */
CREATE INDEX IX__LinkEnd_record ON dbo._LinkEnd(RecordId) INCLUDE (LinkTable, LinkId, [End]);
CREATE INDEX IX__LinkEnd_link   ON dbo._LinkEnd(LinkTable, LinkId);
CREATE INDEX IX_Associate_link1 ON dbo.Associate(Link1);
CREATE INDEX IX_Associate_link2 ON dbo.Associate(Link2);
GO

/* ------------------------------- seed ------------------------------- */
INSERT INTO dbo.Person VALUES
 ('PER0000001', N'Chen',     N'Avery',  '1981-04-02', N'US'),
 ('PER0000002', N'Ríos',     N'Ana',    '1975-11-19', N'ES'),
 ('PER0000003', N'陳',        N'偉明',    '1990-07-30', N'HK'),
 ('PER0000004', N'Ødegård',  N'Zoë',    '1988-02-11', N'NO'),
 ('PER0000005', N'O''Connor', N'Siobhán','1993-03-17', N'IE'),
 ('PER0000006', N'Volkov',   N'Dmitri', NULL,         N'RU');

INSERT INTO dbo.Organisation VALUES
 ('ORG0000001', N'Northwind Logistics', N'Transport'),
 ('ORG0000002', N'Banco Ríos, S.A.',    N'Finance'),
 ('ORG0000003', N'Kowloon Trading Co.', NULL);

INSERT INTO dbo.Vehicle VALUES
 ('VEH0000001', N'AB12 CDE', N'Volvo', N'FH16'),
 ('VEH0000002', N'XY99 ZZZ', N'Ford',  N'Transit'),
 ('VEH0000003', N'HK-4821',  NULL,     NULL);

INSERT INTO dbo.[Event] VALUES
 ('EVT0000900', N'Container seizure, Felixstowe', '2026-02-11T06:30:00.000'),
 ('EVT0000901', N'Wire transfer flagged',         '2026-02-14T11:00:00.000');

/* Associate exercises all three endpoint pairs, AND a parallel pair:
   ASS0000001 and ASS0000002 link the same two people twice. Those must stay two
   separate edges — which only works because an edge id comes from Associate_ID. */
INSERT INTO dbo.Associate VALUES
 ('ASS0000001','PER0000001','PER0000002', N'High',   '2024-01-05','2026-01-05'),
 ('ASS0000002','PER0000001','PER0000002', N'Medium', '2025-06-01','2026-02-01'),
 ('ASS0000003','PER0000003','PER0000006', N'Low',    '2023-03-03', NULL),
 ('ASS0000004','PER0000002','ORG0000002', N'High',   '2018-09-03', NULL),
 ('ASS0000005','PER0000005','ORG0000001', N'Medium', '2021-04-04', NULL),
 ('ASS0000006','ORG0000001','VEH0000001', N'High',   '2022-01-10', NULL),
 ('ORG-ASSOC7','ORG0000003','VEH0000003', N'Low',    NULL,         NULL);

INSERT INTO dbo.Involved_In VALUES
 ('INV0000001', N'Suspect',  '2026-02-11'),
 ('INV0000002', N'Witness',  '2026-02-11'),
 ('INV0000003', N'Conveyance','2026-02-11');

INSERT INTO dbo._LinkEnd (LinkTable, LinkId, [End], RecordId) VALUES
 ('Involved_In','INV0000001',1,'PER0000001'), ('Involved_In','INV0000001',2,'EVT0000900'),
 ('Involved_In','INV0000002',1,'PER0000004'), ('Involved_In','INV0000002',2,'EVT0000900'),
 ('Involved_In','INV0000003',1,'VEH0000001'), ('Involved_In','INV0000003',2,'EVT0000900');

INSERT INTO dbo._AL_Audit ([Action],[User]) VALUES (N'READ', N'analyst1');
INSERT INTO dbo._FTS_Index (RecordId, Term) VALUES ('PER0000001', N'avery');
GO

SELECT 'Person' AS [table], COUNT(*) AS n FROM dbo.Person
UNION ALL SELECT 'Organisation', COUNT(*) FROM dbo.Organisation
UNION ALL SELECT 'Vehicle',      COUNT(*) FROM dbo.Vehicle
UNION ALL SELECT 'Event',        COUNT(*) FROM dbo.[Event]
UNION ALL SELECT 'Associate',    COUNT(*) FROM dbo.Associate
UNION ALL SELECT 'Involved_In',  COUNT(*) FROM dbo.Involved_In
UNION ALL SELECT '_LinkEnd',     COUNT(*) FROM dbo._LinkEnd;
GO
