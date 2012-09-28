import bicycledataprocessor as bdp

def select_runs(riders, maneuvers, environments):
    """Returns a list of runs given a set of conditions.

	Parameters
	----------
	riders : list
		All or a subset of ['Charlie', 'Jason', 'Luke'].
	maneuvers : list
		All or a subset of ['Balance', 'Balance With Disturbance', 'Track Straight Line', 'Track Straight Line With Disturbance'].
	environments : list
		All or a subset of ['Horse Treadmill', 'Pavillion Floor'].

	Returns
	-------
	runs : list
		List of run numbers for the given conditions.

	"""

    dataset = bdp.DataSet()
    dataset.open()

    table = dataset.database.root.runTable

    runs = []
    for row in table.iterrows():
        con = []
        con.append(row['Rider'] in riders)
        con.append(row['Maneuver'] in maneuvers)
        con.append(row['Environment'] in environments)
        con.append(row['corrupt'] is not True)
        con.append(int(row['RunID']) > 100)
        if False not in con:
            runs.append(row['RunID'])

    dataset.close()

    return runs


