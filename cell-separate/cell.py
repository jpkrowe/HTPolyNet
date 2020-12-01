# separate the cell based on the gro coordinate
# 1. Read atoms from gro file
# 2. Build cells based on the system size
# 3. assign cell id to each atoms df

import pandas as pd

class cell(object):
    def __init__(self):
        self.id = ''
        self.center = ''
        self.length = ''

class readGRO(object):
    def __init__(self, name):
        self.groName = name
        self.atomsDF = ''

    def splitRow(self, row, idx=1):
        x = row
        molNum = x[0:5].strip(' ')
        molName = x[5:10].strip(' ')
        atomName = x[10:15].strip(' ')
        globalIdx = x[15:20].strip(' ')
        posX = x[21:28].strip(' ')
        posY = x[29:36].strip(' ')
        posZ = x[37:44].strip(' ')
        data = [molNum, molName, atomName, globalIdx, posX, posY, posZ]
        if idx == 0:
            return data[0]
        elif idx == 1:
            return data[1]
        elif idx == 2:
            return data[2]
        elif idx == 3:
            return data[3]
        elif idx == 4:
            return data[4]
        elif idx == 5:
            return data[5]
        elif idx == 6:
            return data[6]
        else:
            sys.exit()

    def readGRO(self):
        name = self.groName
        columns_name = [0, 'molNum', 'molName', 'atomName', 'globalIdx', 'x', 'y', 'z']
        df = pd.read_csv(name, names=columns_name, header=None, sep='\n', skip_blank_lines=False)
        sysName = df.loc[0][0];
        atNum = df.loc[1][0];
        boxSize = df.iloc[-1][0]
        df1 = df.iloc[2:-1]
        df1.loc[:, 'molNum'] = df1[0].apply(lambda x: self.splitRow(x, idx=0))
        df1.loc[:, 'molName'] = df1[0].apply(lambda x: self.splitRow(x, idx=1))
        df1.loc[:, 'atomName'] = df1[0].apply(lambda x: self.splitRow(x, idx=2))
        df1.loc[:, 'globalIdx'] = df1[0].apply(lambda x: self.splitRow(x, idx=3))
        df1.loc[:, 'posX'] = df1[0].apply(lambda x: self.splitRow(x, idx=4))
        df1.loc[:, 'posY'] = df1[0].apply(lambda x: self.splitRow(x, idx=5))
        df1.loc[:, 'posZ'] = df1[0].apply(lambda x: self.splitRow(x, idx=6))
        df_init = df1[['molNum', 'molName', 'atomName', 'globalIdx', 'posX', 'posY', 'posZ']]
        df_init.reset_index(drop=True, inplace=True)
        self.atomsDF = df_init
        return df_init, sysName, atNum, boxSize

def genCell(boxSize):
    boxSize = '15.000000'
    for i 

if __name__ == '__main__':
    a = readGRO('init-1.gro')
    df_init, sysName, atNum, boxSize = a.readGRO()
    genCell(boxSize.split())
